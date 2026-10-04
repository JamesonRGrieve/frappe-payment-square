# SPDX-License-Identifier: AGPL-3.0-or-later
"""Square Settings + client tests. Webhook vectors are Square's own (square-python-sdk
legacy/tests/utilities/test_webhooks_helper.py). The live charge/refund path is exercised against the
Square sandbox (the go-live gate), not mocked here."""

import frappe
from frappe.tests.utils import FrappeTestCase

from payment_square import square_client
from payment_square.api import payable_request

BODY = (
	'{"merchant_id":"MLEFBHHSJGVHD","type":"webhooks.test_notification","event_id":'
	'"ac3ac95b-f97d-458c-a6e6-18981597e05f","created_at":"2022-07-13T20:30:59.037339943Z","data":'
	'{"type":"webhooks","id":"bc368e64-01aa-407e-b46e-3231809b1129"}}'
)
HEADER = "GF4YkrJgGBDZ9NIYbNXBnMzqb2HoL4RW/S6vkZ9/2N4="
KEY = "Ibxx_5AKakO-3qeNVR61Dw"
URL = "https://webhook.site/679a4f3a-dcfa-49ee-bac5-9d0edad886b9"
GATEWAY = "_Test Square"


class TestSquareClient(FrappeTestCase):
	def test_signature_square_vectors(self):
		self.assertTrue(square_client.is_valid_signature(KEY, URL, BODY, HEADER))
		self.assertTrue(
			square_client.is_valid_signature(
				"signature-key",
				"https://webhook.site/webhooks",
				'{"data":{"type":"webhooks","id":">id<"}}',
				"Cxt7+aTi4rKgcA0bC4g9EHdVtLSDWdqccmL5MvihU4U=",
			)
		)

	def test_signature_rejects_any_mismatch(self):
		self.assertFalse(square_client.is_valid_signature(KEY, URL + "x", BODY, HEADER))
		self.assertFalse(square_client.is_valid_signature("MCmhFRxGX82xMwg5FsaoQA", URL, BODY, HEADER))
		self.assertFalse(square_client.is_valid_signature(KEY, URL, BODY.replace("07-13", "06-13"), HEADER))
		self.assertFalse(square_client.is_valid_signature(KEY, URL, BODY, None))
		self.assertFalse(square_client.is_valid_signature("", URL, BODY, HEADER))

	def test_minor_units(self):
		self.assertEqual(square_client.to_minor_units("12.34", "CAD"), 1234)
		self.assertEqual(square_client.to_minor_units(0.1 + 0.2, "usd"), 30)
		self.assertEqual(square_client.to_minor_units("10.005", "CAD"), 1001)
		self.assertEqual(square_client.to_minor_units(500, "JPY"), 500)
		for bad in (0, -1):
			with self.assertRaises(ValueError):
				square_client.to_minor_units(bad, "CAD")

	def test_idempotency_key_stable_and_bounded(self):
		a = square_client.idempotency_key("ACC-PRQ-2026-00001", "cnon:abc")
		self.assertEqual(a, square_client.idempotency_key("ACC-PRQ-2026-00001", "cnon:abc"))
		self.assertNotEqual(a, square_client.idempotency_key("ACC-PRQ-2026-00001", "cnon:abd"))
		self.assertLessEqual(len(a), square_client.IDEMPOTENCY_KEY_MAX)

	def test_payment_body(self):
		body = square_client.payment_body(
			"cnon:tok",
			"25.50",
			"cad",
			"LOC1",
			"ACC-PRQ-2026-00001",
			note="Sales Order SO-1",
			buyer_email="a@example.com",
		)
		self.assertEqual(body["amount_money"], {"amount": 2550, "currency": "CAD"})
		self.assertEqual(body["source_id"], "cnon:tok")
		self.assertEqual(body["location_id"], "LOC1")
		self.assertEqual(body["reference_id"], "ACC-PRQ-2026-00001")
		self.assertTrue(body["autocomplete"])
		self.assertEqual(body["buyer_email_address"], "a@example.com")
		self.assertNotIn("buyer_email_address", square_client.payment_body("t", 1, "CAD", "L", "R"))
		self.assertEqual(
			len(square_client.payment_body("t", 1, "CAD", "L", "R" * 60)["reference_id"]),
			square_client.REFERENCE_ID_MAX,
		)

	def test_refund_body(self):
		body = square_client.refund_body("PAY1", "10", "CAD", "customer request")
		self.assertEqual(body["payment_id"], "PAY1")
		self.assertEqual(body["amount_money"], {"amount": 1000, "currency": "CAD"})
		self.assertEqual(
			body["idempotency_key"], square_client.refund_body("PAY1", "10", "CAD")["idempotency_key"]
		)


class TestSquareSettings(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
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

	def tearDown(self):
		frappe.db.rollback()

	def test_save_registers_payment_gateway(self):
		self.assertEqual(
			frappe.db.get_value(
				"Payment Gateway", "Square-" + GATEWAY, ["gateway_settings", "gateway_controller"]
			),
			("Square Settings", GATEWAY),
		)

	def test_payment_url_targets_checkout_page(self):
		url = frappe.get_doc("Square Settings", GATEWAY).get_payment_url(
			reference_doctype="Payment Request", reference_docname="ACC-PRQ-1"
		)
		self.assertIn("/square_checkout?", url)
		self.assertIn("reference_docname=ACC-PRQ-1", url)

	def test_currency_validation(self):
		settings = frappe.get_doc("Square Settings", GATEWAY)
		settings.validate_transaction_currency("CAD")
		with self.assertRaises(frappe.ValidationError):
			settings.validate_transaction_currency("INR")

	def test_access_token_is_stored_encrypted(self):
		raw = frappe.db.get_value("Square Settings", GATEWAY, "access_token")
		self.assertNotEqual(raw, "EAAAtest")
		self.assertEqual(frappe.get_doc("Square Settings", GATEWAY).get_password("access_token"), "EAAAtest")

	def test_missing_payment_request_is_404(self):
		with self.assertRaises(frappe.DoesNotExistError):
			payable_request("ACC-PRQ-DOES-NOT-EXIST")
