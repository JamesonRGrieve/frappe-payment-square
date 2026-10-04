# SPDX-License-Identifier: AGPL-3.0-or-later
"""Square Payments API client: the request bodies, money conversion, webhook signature check and the
two HTTP calls this gateway makes (CreatePayment, RefundPayment). No Square SDK: the API surface used
is small, and keeping it here keeps the request shapes reviewable and unit-testable."""

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


def payment_body(source_id, amount, currency, location_id, reference_id, note="", buyer_email=None):
	body = {
		"source_id": source_id,
		"idempotency_key": idempotency_key(reference_id, source_id),
		"amount_money": {"amount": to_minor_units(amount, currency), "currency": currency.upper()},
		"location_id": location_id,
		"reference_id": reference_id[:REFERENCE_ID_MAX],
		"autocomplete": True,
	}
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


def is_valid_signature(signature_key, notification_url, raw_body, signature_header):
	"""Square's webhook check: base64(HMAC-SHA256(key, notification URL + raw body)), constant-time."""
	if not (signature_key and signature_header):
		return False
	digest = hmac.new(signature_key.encode(), (notification_url + raw_body).encode(), hashlib.sha256).digest()
	return hmac.compare_digest(base64.b64encode(digest).decode(), signature_header)


def _post(environment, access_token, path, body):
	response = requests.post(
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


def refund_payment(environment, access_token, body):
	return _post(environment, access_token, "/v2/refunds", body)["refund"]
