# SPDX-License-Identifier: AGPL-3.0-or-later
"""Auction holds (frappe-auction's Authorize at Bid contract) against the real test DB, with Square replaced by
recorded exchanges (fixtures/hold_*.json, validated against Square's OpenAPI document): a hold authorised or
declined and the caller told either way, only the payer may authorise it, and a hold completed for less than,
or all of, what was authorised, or cancelled."""

import frappe
from frappe.tests.utils import FrappeTestCase

from payment_square import holds, square_client
from payment_square.tests.replay import Replay

GATEWAY = "_Test Square Holds"
PAYER = "buyer@example.com"
CALLS = []


def record_authorized(docname, reference):
	CALLS.append(("authorized", docname, reference))


def record_failed(docname, reason):
	CALLS.append(("failed", docname, reason))


class TestHolds(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		CALLS.clear()
		if not frappe.db.exists("User", PAYER):
			frappe.get_doc(
				{
					"doctype": "User",
					"email": PAYER,
					"first_name": "Hold",
					"send_welcome_email": 0,
					"user_type": "Website User",
				}
			).insert(ignore_permissions=True)
		if not frappe.db.exists("Square Settings", GATEWAY):
			frappe.get_doc(
				{
					"doctype": "Square Settings",
					"gateway_name": GATEWAY,
					"environment": "Sandbox",
					"application_id": "sandbox-sq0idb-test",
					"location_id": "LTEST",
					"access_token": "EAAAtest",
				}
			).insert()
		self.settings = frappe.get_doc("Square Settings", GATEWAY)
		self.replay = None

	def tearDown(self):
		square_client.transport = square_client.requests.request
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def square(self, *fixtures):
		self.replay = Replay.of(*fixtures)
		square_client.transport = self.replay
		return self.replay

	def start(self):
		url = self.settings.get_hold_url(
			amount=50.0,
			currency="USD",
			title="Booster box",
			description="Held for your bid on Booster box",
			payer_email=PAYER,
			reference_doctype="Auction Bid",
			reference_docname="bid-1",
			on_authorized="payment_square.tests.test_holds.record_authorized",
			on_failed="payment_square.tests.test_holds.record_failed",
			return_url="https://shop.example/auctions/booster-box",
		)
		self.assertIn("/square_hold?request=", url)
		return url.split("request=", 1)[1]

	def test_an_approved_hold_tells_the_caller_its_payment(self):
		request = self.start()
		self.square("hold_approved")
		result = holds.authorize(request, "cnon:hold", PAYER)
		self.replay.assert_done()
		self.assertEqual(result, {"ok": True, "return_url": "https://shop.example/auctions/booster-box"})
		self.assertEqual(CALLS, [("authorized", "bid-1", "HP1")])
		self.assertEqual(frappe.db.get_value("Integration Request", request, "status"), "Authorized")
		self.assertFalse(holds.authorize(request, "cnon:hold", PAYER)["ok"])

	def test_a_declined_hold_tells_the_caller_it_failed(self):
		request = self.start()
		self.square("hold_declined")
		result = holds.authorize(request, "cnon:declined", PAYER)
		self.replay.assert_done()
		self.assertFalse(result["ok"])
		self.assertEqual([call[:2] for call in CALLS], [("failed", "bid-1")])
		self.assertEqual(frappe.db.get_value("Integration Request", request, "status"), "Failed")

	def test_only_the_payer_may_authorise(self):
		request = self.start()
		with self.assertRaises(frappe.PermissionError):
			holds.authorize(request, "cnon:hold", "someone.else@example.com")

	def test_capturing_less_than_held_lowers_the_payment_first(self):
		self.square("hold_capture_less")
		self.assertEqual(self.settings.capture_hold("HP1", 41.0, "USD"), "HP1")
		self.replay.assert_done()

	def test_capturing_all_of_it_completes_as_held(self):
		self.square("hold_capture_full")
		self.assertEqual(self.settings.capture_hold("HP1", 50.0, "USD"), "HP1")
		self.replay.assert_done()

	def test_capturing_more_than_held_is_refused(self):
		self.square("hold_capture_full")
		with self.assertRaises(square_client.SquareError):
			self.settings.capture_hold("HP1", 60.0, "USD")

	def test_release_cancels(self):
		self.square("hold_release")
		self.settings.release_hold("HP1")
		self.replay.assert_done()

	def test_the_hold_contract_is_complete(self):
		self.assertEqual(self.settings.max_hold_days, square_client.HOLD_DAYS)
		for method in ("get_hold_url", "release_hold", "capture_hold"):
			self.assertTrue(callable(getattr(self.settings, method)))
