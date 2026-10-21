# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whitelisted endpoints: the checkout page's charge call, Square's webhook, and a manager-only
refund. Settling a Payment Request follows frappe/payments (MIT; see NOTICE): run the reference's
``on_payment_authorized`` hook. ERPNext's own Payment Request has no such hook and the webshop's
skips guests, so when the hook leaves the request unpaid this module records the payment itself.

What a Square gift card paid is booked first, as its own Payment Entry to the gift card liability (it
spends a balance the store already owes, it isn't money received); the rest then settles as usual."""

import json

import frappe
from frappe import _
from frappe.utils import fmt_money

from payment_square import square_client
from payment_square.checkout import GIFT_CARD_BRAND
from payment_square.payment_square.doctype.square_settings.square_settings import GATEWAY_PREFIX

PAYABLE_STATUSES = ("Requested", "Initiated", "Partially Paid")
SUCCESS_PAGE = "payment-success"
FAILED_PAGE = "payment-failed"


def payable_request(name):
	"""The submitted, unpaid Payment Request ``name`` and its Square Settings, or a 404/validation error."""
	if not frappe.db.exists("Payment Request", name):
		raise frappe.DoesNotExistError
	request = frappe.get_doc("Payment Request", name)
	gateway = request.payment_gateway or ""
	if request.docstatus != 1 or not gateway.startswith(GATEWAY_PREFIX):
		frappe.throw(_("This payment request cannot be paid with Square."))
	if request.status not in PAYABLE_STATUSES:
		frappe.throw(_("This payment request is already {0}.").format(_(request.status)))
	controller = frappe.db.get_value("Payment Gateway", gateway, "gateway_controller")
	return request, frappe.get_doc("Square Settings", controller)


def book_gift_card_part(request, settings, amount):
	"""A Payment Entry for the part of ``request`` a Square gift card paid: to the gift card liability
	account, with the gift card mode of payment. It leaves the rest of the request outstanding."""
	outstanding = request.outstanding_amount
	request.outstanding_amount = float(amount)
	try:
		entry = request.create_payment_entry(submit=False)
	finally:
		request.outstanding_amount = outstanding
	entry.paid_to = settings.gift_card_liability_account
	entry.mode_of_payment = settings.gift_card_mode_of_payment
	entry.remarks = f"{entry.remarks} (Square gift card)"
	entry.insert(ignore_permissions=True)
	entry.submit()
	request.reload()
	return entry


def settle(request, settings=None, gift_card_amount=0):
	"""Mark ``request`` paid after a completed Square payment; returns where to send the buyer."""
	if gift_card_amount:
		user = frappe.session.user
		try:
			frappe.set_user("Administrator")
			book_gift_card_part(request, settings, gift_card_amount)
		finally:
			frappe.set_user(user)
	redirect_to = request.run_method("on_payment_authorized", "Completed")
	request.reload()
	if request.status != "Paid":
		user = frappe.session.user
		try:
			frappe.set_user("Administrator")  # a guest paying an emailed link may not create a Payment Entry
			request.set_as_paid()
		finally:
			frappe.set_user(user)
	return redirect_to


@frappe.whitelist(allow_guest=True, methods=["POST"])
def make_payment(payment_request, source_id=None, gift_card_source_id=None):
	"""Charge Payment Request ``payment_request`` with the Web Payments SDK token(s): a card
	``source_id``, a Square gift card ``gift_card_source_id``, or both. When the gift card covers only
	part and there is no card, nothing is taken and the buyer is asked for a card for the rest."""
	if not (source_id or gift_card_source_id):
		frappe.throw(_("Enter a card or a gift card."))
	request, settings = payable_request(payment_request)
	result = settings.charge(request, source_id, gift_card_source_id)
	if result.status == "needs_card":
		frappe.db.commit()
		return {
			"needs_card": True,
			"remaining": fmt_money(result.remaining, currency=request.currency),
			"gift_card_amount": fmt_money(result.gift_card_amount, currency=request.currency),
		}
	if not result.completed:
		frappe.db.commit()  # keep the failed Integration Request for the audit trail
		return {"redirect_to": FAILED_PAGE}
	redirect_to = settle(request, settings, result.gift_card_amount)
	frappe.db.commit()
	success = f"{SUCCESS_PAGE}?doctype=Payment%20Request&docname={request.name}"
	return {"redirect_to": redirect_to or success}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def webhook(gateway):
	"""Square webhook (``?gateway=<gateway name>``): settles a completed payment the browser never
	reported back (closed tab, network loss). Unsigned or mis-signed calls are rejected."""
	settings = frappe.get_doc("Square Settings", gateway)
	raw = frappe.request.get_data(as_text=True)
	signature = frappe.get_request_header("x-square-hmacsha256-signature")
	if not square_client.is_valid_signature(
		settings.get_password("webhook_signature_key", raise_exception=False),
		settings.webhook_url or "",
		raw,
		signature,
	):
		raise frappe.PermissionError
	event = json.loads(raw)
	payment = ((event.get("data") or {}).get("object") or {}).get("payment") or {}
	if event.get("type") == "payment.updated" and payment.get("status") == "COMPLETED":
		name = payment.get("reference_id")
		if name and frappe.db.get_value("Payment Request", name, "status") in PAYABLE_STATUSES:
			request = frappe.get_doc("Payment Request", name)
			if is_whole_card_payment(payment, request):
				settle(request)
				frappe.db.commit()
			else:
				# A gift card payment, or one part of a split: booking it here could book the gift card
				# part as money received or the request twice, so it is left for an accountant.
				frappe.log_error(
					title=f"Square payment {payment.get('id')} for {name} needs settling by hand",
					message="A gift card or split payment completed without the checkout settling it.",
				)
	return {"ok": True}


def is_whole_card_payment(payment, request):
	"""A completed card payment of the request's whole amount (not a gift card, not part of a split)."""
	brand = ((payment.get("card_details") or {}).get("card") or {}).get("card_brand")
	whole = square_client.to_minor_units(request.grand_total, request.currency)
	return brand != GIFT_CARD_BRAND and int((payment.get("amount_money") or {}).get("amount") or 0) == whole


@frappe.whitelist(methods=["POST"])
def refund(payment_request, reason=""):
	"""Refund the Square payment that paid ``payment_request`` in full (Accounts Manager only). The
	accounting reversal (a return Payment Entry) stays a deliberate manual step in ERPNext."""
	frappe.only_for(("Accounts Manager", "System Manager"))
	request = frappe.get_doc("Payment Request", payment_request)
	log = frappe.get_all(
		"Integration Request",
		filters={
			"reference_doctype": "Payment Request",
			"reference_docname": request.name,
			"integration_request_service": "Square",
			"status": "Completed",
		},
		fields=["name", "output"],
		order_by="creation desc",
		limit=1,
	)
	output = json.loads(log[0].output or "{}") if log else {}
	# Each payment back to where it came from: a gift card part to the gift card, the card part to the card.
	payments = output.get("payments") or (
		[{"id": output["payment_id"], "amount": str(request.grand_total)}] if output.get("payment_id") else []
	)
	if not payments:
		frappe.throw(_("No completed Square payment found for {0}.").format(request.name))
	controller = frappe.db.get_value("Payment Gateway", request.payment_gateway, "gateway_controller")
	settings = frappe.get_doc("Square Settings", controller)
	results = [settings.refund(p["id"], p["amount"], request.currency, reason) for p in payments]
	entry = frappe.get_doc("Integration Request", log[0].name)
	for result in results:
		entry.add_comment("Info", f"Refund {result.get('id')}: {result.get('status')}")
	return {
		"refund_id": results[0].get("id"),
		"status": results[0].get("status"),
		"refunds": [{"id": r.get("id"), "status": r.get("status")} for r in results],
	}
