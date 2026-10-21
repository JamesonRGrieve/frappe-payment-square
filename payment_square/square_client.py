# SPDX-License-Identifier: AGPL-3.0-or-later
"""Square Payments API client: the request bodies, money conversion, webhook signature check and the
HTTP calls this gateway makes (CreatePayment, CompletePayment, CancelPayment, PayOrder, RefundPayment).
No Square SDK: the API surface used is small, and keeping it here keeps the request shapes reviewable and
unit-testable (tools/validate_requests.py checks them against Square's published OpenAPI document)."""

import base64
import hashlib
import hmac
from decimal import ROUND_HALF_UP, Decimal

import requests

API_VERSION = "2026-09-16"  # Square-Version header the request shapes were written against
BASE_URLS = {"Sandbox": "https://connect.squareupsandbox.com", "Production": "https://connect.squareup.com"}
WEB_SDK_URLS = {
	"Sandbox": "https://sandbox.web.squarecdn.com/v1/square.js",
	"Production": "https://web.squarecdn.com/v1/square.js",
}
REQUEST_TIMEOUT_SECONDS = 30
IDEMPOTENCY_KEY_MAX = 45  # Square's limit
REFERENCE_ID_MAX = 40
NOTE_MAX = 500
# ISO 4217 currencies without minor units (Square amounts are in the smallest unit).
ZERO_DECIMAL_CURRENCIES = frozenset({"JPY", "KRW", "CLP", "ISK", "VND", "XAF", "XOF", "XPF"})


class SquareError(Exception):
	"""A Square API call failed; ``detail`` is Square's error list (safe to log, never shown to buyers)."""

	def __init__(self, message, detail=None):
		super().__init__(message)
		self.detail = detail


def to_minor_units(amount, currency):
	"""``amount`` (a decimal-like major-unit value) as Square's integer smallest-unit amount."""
	exponent = 0 if currency.upper() in ZERO_DECIMAL_CURRENCIES else 2
	scaled = Decimal(str(amount)).scaleb(exponent).quantize(Decimal(1), rounding=ROUND_HALF_UP)
	if scaled <= 0:
		raise ValueError(f"amount must be positive, got {amount} {currency}")
	return int(scaled)


def idempotency_key(*parts):
	"""A stable key from ``parts``: retrying the same attempt reuses it, so Square charges once."""
	return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:IDEMPOTENCY_KEY_MAX]


def payment_body(
	source_id,
	amount,
	currency,
	location_id,
	reference_id,
	note="",
	buyer_email=None,
	*,
	order_id=None,
	split=False,
	gift_card=False,
):
	"""A CreatePayment body. A payment that is one of several for the same checkout (``split``) is held
	approved, to be completed (or paid onto its order) once every part is approved, or cancelled. A Square
	gift card in a split may approve less than asked (partial authorisation), the rest paid by card."""
	body = {
		"source_id": source_id,
		"idempotency_key": idempotency_key(reference_id, source_id, amount),
		"amount_money": {"amount": to_minor_units(amount, currency), "currency": currency.upper()},
		"location_id": location_id,
		"reference_id": reference_id[:REFERENCE_ID_MAX],
		"autocomplete": not split,
	}
	if split and gift_card:
		body["accept_partial_authorization"] = True
	if order_id:
		body["order_id"] = order_id
	if note:
		body["note"] = note[:NOTE_MAX]
	if buyer_email:
		body["buyer_email_address"] = buyer_email
	return body


def refund_body(payment_id, amount, currency, reason=""):
	body = {
		"idempotency_key": idempotency_key("refund", payment_id, amount),
		"payment_id": payment_id,
		"amount_money": {"amount": to_minor_units(amount, currency), "currency": currency.upper()},
	}
	if reason:
		body["reason"] = reason[:192]
	return body


def pay_order_body(order_id, payment_ids):
	return {
		"idempotency_key": idempotency_key("pay", order_id, *payment_ids),
		"payment_ids": list(payment_ids),
	}


def from_minor_units(money):
	"""Square Money → Decimal major units."""
	exponent = 0 if (money.get("currency") or "").upper() in ZERO_DECIMAL_CURRENCIES else 2
	return Decimal(int(money.get("amount") or 0)).scaleb(-exponent)


def is_valid_signature(signature_key, notification_url, raw_body, signature_header):
	"""Square's webhook check: base64(HMAC-SHA256(key, notification URL + raw body)), constant-time."""
	if not (signature_key and signature_header):
		return False
	digest = hmac.new(signature_key.encode(), (notification_url + raw_body).encode(), hashlib.sha256).digest()
	return hmac.compare_digest(base64.b64encode(digest).decode(), signature_header)


# The HTTP transport (``requests.post``'s signature). Tests replace it with a replay of recorded
# exchanges validated against Square's OpenAPI document (tests/replay.py); nothing else may.
transport = requests.post


def _post(environment, access_token, path, body):
	response = transport(
		BASE_URLS[environment] + path,
		json=body,
		headers={
			"Authorization": f"Bearer {access_token}",
			"Square-Version": API_VERSION,
			"Content-Type": "application/json",
		},
		timeout=REQUEST_TIMEOUT_SECONDS,
	)
	data = response.json() if response.content else {}
	if response.status_code >= 400 or data.get("errors"):
		raise SquareError(f"Square {path} failed with HTTP {response.status_code}", data.get("errors"))
	return data


def create_payment(environment, access_token, body):
	return _post(environment, access_token, "/v2/payments", body)["payment"]


def complete_payment(environment, access_token, payment_id):
	return _post(environment, access_token, f"/v2/payments/{payment_id}/complete", {})["payment"]


def cancel_payment(environment, access_token, payment_id):
	return _post(environment, access_token, f"/v2/payments/{payment_id}/cancel", None)["payment"]


def pay_order(environment, access_token, order_id, payment_ids):
	return _post(
		environment, access_token, f"/v2/orders/{order_id}/pay", pay_order_body(order_id, payment_ids)
	)["order"]


def refund_payment(environment, access_token, body):
	return _post(environment, access_token, "/v2/refunds", body)["refund"]
