# SPDX-License-Identifier: AGPL-3.0-or-later
"""Checkout against the real ERPNext test DB, with Square replaced by recorded exchanges (fixtures/,
validated against Square's OpenAPI document): a card alone, a Square gift card alone, a gift card with a
card for the rest, a gift card that needs a card, a declined card after a gift card authorisation, how each
is booked (the gift card part to the gift card liability, never as money received), the webhook's guard,
and refunding a split payment part by part."""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from payment_square import api, checkout, square_client
from payment_square.tests.replay import Replay

GATEWAY = "_Test Square Checkout"
COMPANY = "_Test Company 1"
BANK = "_Test Bank USD - _TC1"
LIABILITY = "_Test Square Gift Cards - _TC1"
GIFT_MODE = "_Test Square Gift Card"
EMAIL = "buyer@example.com"


def ensure_liability_account():
	if frappe.db.exists("Account", LIABILITY):
		return
	parent = frappe.db.get_value(
		"Account",
		{"company": COMPANY, "root_type": "Liability", "is_group": 1, "account_name": "Current Liabilities"},
	) or frappe.db.get_value("Account", {"company": COMPANY, "root_type": "Liability", "is_group": 1})
	frappe.get_doc(
		{
			"doctype": "Account",
			"account_name": "_Test Square Gift Cards",
			"company": COMPANY,
			"parent_account": parent,
			"root_type": "Liability",
			"account_currency": "USD",
		}
	).insert(ignore_permissions=True)


class TestCheckout(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		ensure_liability_account()
		if not frappe.db.exists("Mode of Payment", GIFT_MODE):
			frappe.get_doc(
				{"doctype": "Mode of Payment", "mode_of_payment": GIFT_MODE, "type": "General"}
			).insert()
		if not frappe.db.exists("Square Settings", GATEWAY):
			frappe.get_doc(
				{
					"doctype": "Square Settings",
					"gateway_name": GATEWAY,
					"environment": "Sandbox",
					"application_id": "sandbox-sq0idb-test",
					"location_id": "LTEST",
					"access_token": "EAAAtest",
					"accept_gift_cards": 1,
					"gift_card_liability_account": LIABILITY,
					"gift_card_mode_of_payment": GIFT_MODE,
				}
			).insert()
		gateway = "Square-" + GATEWAY
		self.account = frappe.db.get_value("Payment Gateway Account", {"payment_gateway": gateway})
		if not self.account:
			self.account = (
				frappe.get_doc(
					{
						"doctype": "Payment Gateway Account",
						"payment_gateway": gateway,
						"company": COMPANY,
						"payment_account": BANK,
						"currency": "USD",
					}
				)
				.insert()
				.name
			)
		self.request = self.payment_request()
		self.replay = None
		self.order_hook = checkout.square_order_id

	def tearDown(self):
		square_client.transport = square_client.requests.post
		checkout.square_order_id = self.order_hook
		frappe.db.rollback()

	def payment_request(self):
		from erpnext.accounts.doctype.payment_request.payment_request import make_payment_request
		from erpnext.selling.doctype.sales_order.test_sales_order import make_sales_order

		order = make_sales_order(
			company=COMPANY,
			warehouse="Stores - _TC1",
			customer="_Test Customer",
			currency="USD",
			item_code="_Test Item",
			qty=1,
			rate=30,
		)
		request = make_payment_request(
			dt="Sales Order",
			dn=order.name,
			payment_gateway_account=self.account,
			recipient_id=EMAIL,
			submit_doc=1,
			return_doc=1,
			mute_email=1,
		)
		self.assertEqual((request.grand_total, request.currency), (30, "USD"))
		return request

	def square(self, *fixtures):
		self.replay = Replay.of(*fixtures)
		square_client.transport = self.replay
		return self.replay

	def entries(self):
		"""(paid_to, mode of payment, amount) of each Payment Entry made for the request."""
		names = frappe.get_all(
			"Payment Entry Reference",
			filters={"payment_request": self.request.name, "docstatus": 1},
			pluck="parent",
		)
		return sorted(
			tuple(frappe.db.get_value("Payment Entry", name, ["paid_to", "mode_of_payment", "paid_amount"]))
			for name in set(names)
		)

	def status(self):
		return frappe.db.get_value("Payment Request", self.request.name, "status")

	def test_a_card_pays_the_whole_request(self):
		self.square("card_only")
		result = api.make_payment(self.request.name, source_id="cnon:card")
		self.replay.assert_done()
		self.assertIn("payment-success", result["redirect_to"])
		self.assertEqual(self.status(), "Paid")
		self.assertEqual([(paid_to, amount) for paid_to, _m, amount in self.entries()], [(BANK, 30)])

	def test_a_gift_card_alone_is_booked_to_the_liability(self):
		self.square("gift_card_whole")
		api.make_payment(self.request.name, gift_card_source_id="gftc:whole")
		self.replay.assert_done()
		self.assertEqual(self.status(), "Paid")
		self.assertEqual(self.entries(), [(LIABILITY, GIFT_MODE, 30)])

	def test_a_split_books_each_part_and_refunds_each_part(self):
		self.square("split")
		api.make_payment(self.request.name, source_id="cnon:rest", gift_card_source_id="gftc:part")
		self.replay.assert_done()
		self.assertEqual(self.status(), "Paid")
		entries = {paid_to: amount for paid_to, _m, amount in self.entries()}
		self.assertEqual(entries, {LIABILITY: 10, BANK: 20})
		log = frappe.get_all(
			"Integration Request",
			filters={"reference_docname": self.request.name, "status": "Completed"},
			pluck="output",
		)
		self.assertEqual(json.loads(log[0])["payment_ids"], ["GP2", "CP2"])
		self.square("refund_split")
		result = api.refund(self.request.name, reason="Returned")
		self.replay.assert_done()
		self.assertEqual([r["id"] for r in result["refunds"]], ["RF-G", "RF-C"])

	def test_with_a_square_order_both_payments_are_paid_onto_it(self):
		# The square_payment_order hook (frappe-square-sync) returned the webshop order's Square id.
		checkout.square_order_id = lambda settings, request: "SQ-ORD-1"
		self.square("split_with_order")
		api.make_payment(self.request.name, source_id="cnon:rest", gift_card_source_id="gftc:part")
		self.replay.assert_done()
		self.assertEqual(self.status(), "Paid")

	def test_a_gift_card_short_of_the_total_asks_for_a_card_and_takes_nothing(self):
		self.square("needs_card")
		result = api.make_payment(self.request.name, gift_card_source_id="gftc:part")
		self.replay.assert_done()
		self.assertTrue(result["needs_card"])
		self.assertIn("20", result["remaining"])
		self.assertEqual(self.status(), "Requested")
		self.assertEqual(self.entries(), [])

	def test_a_declined_card_cancels_the_gift_card_authorisation(self):
		self.square("card_declined")
		result = api.make_payment(
			self.request.name, source_id="cnon:declined", gift_card_source_id="gftc:part"
		)
		self.replay.assert_done()
		self.assertEqual(result["redirect_to"], api.FAILED_PAGE)
		self.assertEqual(self.entries(), [])

	def test_gift_cards_must_be_switched_on(self):
		frappe.db.set_value("Square Settings", GATEWAY, "accept_gift_cards", 0)
		with self.assertRaises(frappe.ValidationError):
			api.make_payment(self.request.name, gift_card_source_id="gftc:whole")

	def test_the_webhook_settles_only_a_whole_card_payment(self):
		whole = {
			"amount_money": {"amount": 3000, "currency": "USD"},
			"card_details": {"card": {"card_brand": "VISA"}},
		}
		self.assertTrue(api.is_whole_card_payment(whole, self.request))
		gift = {**whole, "card_details": {"card": {"card_brand": "SQUARE_GIFT_CARD"}}}
		self.assertFalse(api.is_whole_card_payment(gift, self.request))
		part = {**whole, "amount_money": {"amount": 2000, "currency": "USD"}}
		self.assertFalse(api.is_whole_card_payment(part, self.request))
