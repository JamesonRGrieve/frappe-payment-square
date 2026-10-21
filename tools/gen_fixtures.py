# SPDX-License-Identifier: AGPL-3.0-or-later
"""Writes the recorded Square exchanges the checkout tests replay (payment_square/tests/fixtures/*.json).
Every shape here is checked against Square's OpenAPI document by ``python3 -m tools.validate_fixtures``;
run that after ``python3 -m tools.gen_fixtures``."""

import json
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "payment_square" / "tests" / "fixtures"
ANY = "<any>"
LOCATION = "LTEST"
EMAIL = "buyer@example.com"
CREATED = "2026-10-20T10:00:00Z"


def m(amount):
	return {"amount": amount, "currency": "USD"}


def write(name, description, exchanges):
	OUT.mkdir(parents=True, exist_ok=True)
	(OUT / f"{name}.json").write_text(
		json.dumps({"description": description, "exchanges": exchanges}, indent=1) + "\n"
	)


def create(source, amount, *, split, gift_card=False):
	body = {
		"source_id": source,
		"idempotency_key": ANY,
		"amount_money": m(amount),
		"location_id": LOCATION,
		"reference_id": ANY,
		"autocomplete": not split,
		"note": ANY,
		"buyer_email_address": EMAIL,
	}
	if split and gift_card:
		body["accept_partial_authorization"] = True
	return body


def payment(pid, status, amount, brand="VISA"):
	return {
		"id": pid,
		"created_at": CREATED,
		"updated_at": CREATED,
		"amount_money": m(amount),
		"approved_money": m(amount),
		"status": status,
		"source_type": "CARD",
		"card_details": {
			"status": "AUTHORIZED" if status == "APPROVED" else "CAPTURED",
			"card": {"card_brand": brand, "last_4": "0000"},
		},
		"location_id": LOCATION,
	}


def charge(source, amount, response, *, split, gift_card=False, status=200, order_id=None):
	request = create(source, amount, split=split, gift_card=gift_card)
	if order_id:
		request["order_id"] = order_id
	exchange = {"method": "POST", "path": "/v2/payments", "request": request, "response": response}
	if status != 200:
		exchange["status"] = status
	return exchange


def complete(pid, amount, brand="VISA"):
	return {
		"method": "POST",
		"path": f"/v2/payments/{pid}/complete",
		"request": {},
		"response": {"payment": payment(pid, "COMPLETED", amount, brand)},
	}


def cancel(pid, amount):
	voided = payment(pid, "CANCELED", amount, "SQUARE_GIFT_CARD")
	voided["card_details"]["status"] = "VOIDED"
	return {
		"method": "POST",
		"path": f"/v2/payments/{pid}/cancel",
		"request": None,
		"response": {"payment": voided},
	}


GIFT = "SQUARE_GIFT_CARD"
write(
	"card_only",
	"A card pays the whole request, completed at once.",
	[charge("cnon:card", 3000, {"payment": payment("CP0", "COMPLETED", 3000)}, split=False)],
)
write(
	"gift_card_whole",
	"A gift card covers the whole request: authorised with partial authorisation allowed, then completed.",
	[
		charge(
			"gftc:whole",
			3000,
			{"payment": payment("GP1", "APPROVED", 3000, GIFT)},
			split=True,
			gift_card=True,
		),
		complete("GP1", 3000, GIFT),
	],
)
write(
	"split",
	"A gift card covers 10.00 of 30.00; the card pays the other 20.00; both are completed together.",
	[
		charge(
			"gftc:part", 3000, {"payment": payment("GP2", "APPROVED", 1000, GIFT)}, split=True, gift_card=True
		),
		charge("cnon:rest", 2000, {"payment": payment("CP2", "APPROVED", 2000)}, split=True),
		complete("GP2", 1000, GIFT),
		complete("CP2", 2000),
	],
)
write(
	"needs_card",
	"A gift card covers only part and there is no card: its authorisation is cancelled, nothing is taken.",
	[
		charge(
			"gftc:part", 3000, {"payment": payment("GP3", "APPROVED", 1000, GIFT)}, split=True, gift_card=True
		),
		cancel("GP3", 1000),
	],
)
write(
	"card_declined",
	"The card for the rest is declined: the gift card authorisation is cancelled.",
	[
		charge(
			"gftc:part", 3000, {"payment": payment("GP4", "APPROVED", 1000, GIFT)}, split=True, gift_card=True
		),
		charge(
			"cnon:declined",
			2000,
			{
				"errors": [
					{"category": "PAYMENT_METHOD_ERROR", "code": "CARD_DECLINED", "detail": "Card declined."}
				]
			},
			split=True,
			status=400,
		),
		cancel("GP4", 1000),
	],
)


def refund(payment_id, amount, rid):
	return {
		"method": "POST",
		"path": "/v2/refunds",
		"request": {
			"idempotency_key": ANY,
			"payment_id": payment_id,
			"amount_money": m(amount),
			"reason": "Returned",
		},
		"response": {
			"refund": {
				"id": rid,
				"status": "PENDING",
				"location_id": LOCATION,
				"amount_money": m(amount),
				"payment_id": payment_id,
				"created_at": CREATED,
				"updated_at": CREATED,
			}
		},
	}


write(
	"refund_split",
	"Refunding a split payment: each part back to where it came from (the gift card part to the gift card).",
	[refund("GP2", 1000, "RF-G"), refund("CP2", 2000, "RF-C")],
)


write(
	"split_with_order",
	"The webshop order exists in Square (frappe-square-sync): both payments are taken against it and paid onto it together.",
	[
		charge(
			"gftc:part",
			3000,
			{"payment": payment("GP5", "APPROVED", 1000, GIFT)},
			split=True,
			gift_card=True,
			order_id="SQ-ORD-1",
		),
		charge(
			"cnon:rest", 2000, {"payment": payment("CP5", "APPROVED", 2000)}, split=True, order_id="SQ-ORD-1"
		),
		{
			"method": "POST",
			"path": "/v2/orders/SQ-ORD-1/pay",
			"request": {"idempotency_key": ANY, "payment_ids": ["GP5", "CP5"]},
			"response": {
				"order": {
					"id": "SQ-ORD-1",
					"location_id": LOCATION,
					"state": "COMPLETED",
					"version": 3,
					"total_money": m(3000),
				}
			},
		},
	],
)
