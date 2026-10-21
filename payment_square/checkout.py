# SPDX-License-Identifier: AGPL-3.0-or-later
"""Taking one Payment Request's money in Square: by card, by Square gift card, or split between the two.

- Before charging, the ``square_payment_order`` hook (frappe-square-sync) may return a Square order for the
  request's document; the payments are then taken against it, so Square sees the sale itself.
- A card alone is one payment, completed at once.
- A gift card is authorised for the whole amount with partial authorisation allowed. If it covers it, it is
  completed (or paid onto the order). If it covers only part, the card pays the rest; both are held
  approved, then completed together. Without a card for the rest, the gift card authorisation is cancelled
  and the buyer is asked for a card: nothing is taken until the whole amount is approved.
- Any failure after an authorisation cancels every authorisation already made, so the buyer is never
  charged for an unpaid request.

Every attempt is an Integration Request (service ``Square``) whose output names all its payment ids, so
frappe-square-sync recognises (and skips) them."""

from dataclasses import dataclass, field
from decimal import Decimal

import frappe
from frappe.integrations.utils import create_request_log

from payment_square import square_client

SERVICE_NAME = "Square"
ORDER_HOOK = "square_payment_order"
GIFT_CARD_BRAND = "SQUARE_GIFT_CARD"
COMPLETED = "COMPLETED"
APPROVED = "APPROVED"


@dataclass
class Checkout:
	"""What a charge did. ``status`` is ``completed``, ``needs_card`` (the gift card covers only part:
	``remaining`` is due by card) or ``failed``."""

	status: str
	payment_ids: list = field(default_factory=list)
	gift_card_amount: Decimal = Decimal(0)
	card_amount: Decimal = Decimal(0)
	remaining: Decimal = Decimal(0)

	@property
	def completed(self):
		return self.status == "completed"

	def payments(self):
		"""Each payment taken and its amount (gift card first), for refunds."""
		amounts = [a for a in (self.gift_card_amount, self.card_amount) if a]
		return [
			{"id": payment_id, "amount": str(amount)}
			for payment_id, amount in zip(self.payment_ids, amounts, strict=False)
		]


def square_order_id(settings, payment_request):
	"""The Square order to take the payment against, from the first ``square_payment_order`` hook that
	returns one (frappe-square-sync creates it), or None."""
	for method in frappe.get_hooks(ORDER_HOOK):
		order_id = frappe.get_attr(method)(settings.gateway_name, payment_request)
		if order_id:
			return order_id
	return None


class Charge:
	"""One charge attempt for ``payment_request`` with ``settings`` (a Square Settings)."""

	def __init__(self, settings, payment_request):
		self.settings, self.request = settings, payment_request
		self.amount = Decimal(str(payment_request.grand_total))
		self.currency = payment_request.currency
		self.token = settings.get_password("access_token")
		self.approved = []  # payment ids authorised and not yet completed

	def body(self, source_id, amount, *, order_id, split, gift_card=False):
		return square_client.payment_body(
			source_id,
			amount,
			self.currency,
			self.settings.location_id,
			reference_id=self.request.name,
			note=f"{self.request.reference_doctype} {self.request.reference_name}",
			buyer_email=self.request.email_to or None,
			order_id=order_id,
			split=split,
			gift_card=gift_card,
		)

	def create(self, body):
		payment = square_client.create_payment(self.settings.environment, self.token, body)
		if payment.get("status") == APPROVED:
			self.approved.append(payment["id"])
		return payment

	def release(self):
		"""Cancel every authorisation still held (the buyer pays nothing for an unpaid request)."""
		for payment_id in self.approved:
			try:
				square_client.cancel_payment(self.settings.environment, self.token, payment_id)
			except square_client.SquareError, OSError:
				frappe.log_error(
					title=f"Square authorisation {payment_id} not cancelled", message=frappe.get_traceback()
				)
		self.approved = []

	def finish(self, order_id):
		"""Capture what is held: pay the order with all of it, or complete each payment."""
		if order_id:
			square_client.pay_order(self.settings.environment, self.token, order_id, self.approved)
		else:
			for payment_id in self.approved:
				square_client.complete_payment(self.settings.environment, self.token, payment_id)
		self.approved = []

	def run(self, source_id=None, gift_card_source_id=None):
		order_id = square_order_id(self.settings, self.request)
		if not gift_card_source_id:
			payment = self.create(self.body(source_id, self.amount, order_id=order_id, split=False))
			done = payment.get("status") == COMPLETED
			return Checkout("completed" if done else "failed", [payment["id"]], card_amount=self.amount)
		gift = self.create(
			self.body(gift_card_source_id, self.amount, order_id=order_id, split=True, gift_card=True)
		)
		if gift.get("status") != APPROVED:
			return Checkout("failed", [gift["id"]])
		covered = square_client.from_minor_units(gift.get("amount_money") or {})
		remaining = self.amount - covered
		if remaining <= 0:
			self.finish(order_id)
			return Checkout("completed", [gift["id"]], gift_card_amount=self.amount)
		if not source_id:
			self.release()
			return Checkout("needs_card", [gift["id"]], gift_card_amount=covered, remaining=remaining)
		card = self.create(self.body(source_id, remaining, order_id=order_id, split=True))
		if card.get("status") != APPROVED:
			self.release()
			return Checkout("failed", [gift["id"], card["id"]])
		self.finish(order_id)
		return Checkout(
			"completed", [gift["id"], card["id"]], gift_card_amount=covered, card_amount=remaining
		)


def charge(settings, payment_request, source_id=None, gift_card_source_id=None):
	"""Take ``payment_request``'s own amount (never a client value), logged as an Integration Request."""
	settings.validate_transaction_currency(payment_request.currency)
	log = create_request_log(
		{
			"payment_request": payment_request.name,
			"amount": payment_request.grand_total,
			"currency": payment_request.currency,
			"gift_card": bool(gift_card_source_id),
		},
		service_name=SERVICE_NAME,
		reference_doctype="Payment Request",
		reference_docname=payment_request.name,
	)
	attempt = Charge(settings, payment_request)
	try:
		result = attempt.run(source_id, gift_card_source_id)
	except square_client.SquareError, OSError, ValueError:
		attempt.release()
		log.db_set({"status": "Failed", "error": "Square declined or failed"}, update_modified=False)
		frappe.log_error(title="Square payment failed", message=frappe.get_traceback())
		return Checkout("failed")
	log.db_set(
		{
			"status": "Completed"
			if result.completed
			else ("Queued" if result.status == "needs_card" else "Failed"),
			"output": frappe.as_json(
				{
					"payment_id": result.payment_ids[-1] if result.payment_ids else None,
					"payment_ids": result.payment_ids,
					"payments": result.payments() if result.completed else [],
					"status": result.status,
					"gift_card_amount": str(result.gift_card_amount),
					"card_amount": str(result.card_amount),
				}
			),
		},
		update_modified=False,
	)
	return result
