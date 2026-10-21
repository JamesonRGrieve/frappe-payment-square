# SPDX-License-Identifier: AGPL-3.0-or-later
# Gateway controller shape adapted from frappe/payments' Stripe Settings (MIT, Frappe Technologies);
# see NOTICE.
"""A Square merchant account as a Frappe Payment Gateway. ERPNext's Payment Request calls
``validate_transaction_currency`` and ``get_payment_url``; the checkout page tokenises the card in
the browser and ``charge`` takes the payment for the Payment Request's own amount."""

from urllib.parse import urlencode

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import call_hook_method, get_url

from payment_square import square_client

GATEWAY_PREFIX = "Square-"
# Square accepts payments only in the seller account's own currency; these are the countries it
# serves, so a mismatched currency fails fast here instead of at Square.
SUPPORTED_CURRENCIES = frozenset({"CAD", "USD", "GBP", "EUR", "AUD", "JPY"})


def create_payment_gateway(gateway, settings, controller):
	"""Register ``gateway`` as a Payment Gateway (same contract as frappe/payments' helper)."""
	if not frappe.db.exists("Payment Gateway", gateway):
		frappe.get_doc(
			{
				"doctype": "Payment Gateway",
				"gateway": gateway,
				"gateway_settings": settings,
				"gateway_controller": controller,
			}
		).insert(ignore_permissions=True)


class SquareSettings(Document):
	def on_update(self):
		gateway = GATEWAY_PREFIX + self.gateway_name
		create_payment_gateway(gateway, settings="Square Settings", controller=self.gateway_name)
		call_hook_method("payment_gateway_enabled", gateway=gateway)

	def validate_transaction_currency(self, currency):
		if currency not in SUPPORTED_CURRENCIES:
			frappe.throw(_("Square does not support transactions in currency {0}").format(currency))

	def get_payment_url(self, **kwargs):
		return get_url(f"./square_checkout?{urlencode(kwargs)}")

	def charge(self, payment_request, source_id=None, gift_card_source_id=None):
		"""Charge ``payment_request`` (a submitted Payment Request using this gateway) with the browser
		token(s): a card ``source_id``, a Square gift card ``gift_card_source_id``, or both (see
		payment_square.checkout). The amount always comes from the Payment Request, never the client."""
		from payment_square import checkout

		if gift_card_source_id and not self.accept_gift_cards:
			frappe.throw(_("Square gift cards aren't accepted here."))
		return checkout.charge(self, payment_request, source_id, gift_card_source_id)

	def refund(self, payment_id, amount, currency, reason=""):
		body = square_client.refund_body(payment_id, amount, currency, reason)
		return square_client.refund_payment(self.environment, self.get_password("access_token"), body)
