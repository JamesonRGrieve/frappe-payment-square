# SPDX-License-Identifier: AGPL-3.0-or-later
"""Card holds for frappe-auction's "Authorize at Bid" (the contract in frappe-auction's ``auction/holds.py``):
the buyer's card is authorised for an amount and not charged; the hold is completed later for at most that
amount, or cancelled. Square cancels an uncompleted hold itself after ``square_client.HOLD_DAYS``.

Each hold is an Integration Request (service ``Square Hold``) holding what the caller asked for: the amount,
currency, payer, the document it is for, and the dotted paths to call once Square approves or refuses it."""

from decimal import Decimal

import frappe
from frappe import _
from frappe.integrations.utils import create_request_log
from frappe.utils import get_url

from payment_square import square_client

SERVICE_NAME = "Square Hold"
APPROVED = "APPROVED"


def start(settings, **request):
	"""Records a hold request and returns the page where the buyer authorises it."""
	# The request's data names what the hold is for; the link is set too when that DocType exists on this site.
	linked = frappe.db.exists("DocType", request["reference_doctype"])
	log = create_request_log(
		{**request, "amount": str(request["amount"]), "gateway_settings": settings.name},
		service_name=SERVICE_NAME,
		reference_doctype=request["reference_doctype"] if linked else None,
		reference_docname=request["reference_docname"] if linked else None,
	)
	return get_url(f"/square_hold?request={log.name}")


def pending(request_name, user):
	"""The hold request `user` may authorise now, and its gateway settings."""
	log = frappe.get_doc("Integration Request", request_name)
	if log.integration_request_service != SERVICE_NAME:
		raise frappe.DoesNotExistError
	data = frappe.parse_json(log.data)
	if data.get("payer_email") != user:
		raise frappe.PermissionError
	return log, data, frappe.get_doc("Square Settings", data["gateway_settings"])


def authorize(request_name, source_id, user):
	"""Authorises the request's amount on the buyer's tokenised card, then tells the caller."""
	log, data, settings = pending(request_name, user)
	if log.status != "Queued":
		return {
			"ok": False,
			"error": _("This hold has already been handled."),
			"return_url": data["return_url"],
		}
	body = square_client.hold_body(
		source_id,
		Decimal(data["amount"]),
		data["currency"],
		settings.location_id,
		data["reference_docname"],
		note=data.get("description") or "",
		buyer_email=user,
	)
	try:
		payment = square_client.create_payment(
			settings.environment, settings.get_password("access_token"), body
		)
	except square_client.SquareError as error:
		payment, refusal = None, str(error)
	if payment and payment.get("status") == APPROVED:
		log.db_set(
			{"status": "Authorized", "output": frappe.as_json({"payment_id": payment["id"]})},
			update_modified=False,
		)
		frappe.get_attr(data["on_authorized"])(data["reference_docname"], payment["id"])
		return {"ok": True, "return_url": data["return_url"]}
	if payment:
		square_client.cancel_payment(
			settings.environment, settings.get_password("access_token"), payment["id"]
		)
		refusal = _("Square didn't approve the hold.")
	log.db_set({"status": "Failed", "error": refusal}, update_modified=False)
	frappe.get_attr(data["on_failed"])(data["reference_docname"], _("Your card didn't authorise the amount."))
	return {
		"ok": False,
		"error": _("Your card didn't authorise the amount."),
		"return_url": data["return_url"],
	}


def release(settings, payment_id):
	square_client.cancel_payment(settings.environment, settings.get_password("access_token"), payment_id)


def capture(settings, payment_id, amount, currency):
	"""Completes a held payment for `amount`, lowering it first when less than was authorised."""
	token = settings.get_password("access_token")
	held = square_client.get_payment(settings.environment, token, payment_id)
	authorised = square_client.from_minor_units(held.get("approved_money") or held["amount_money"])
	if Decimal(str(amount)) > authorised:
		raise square_client.SquareError(f"Capture of {amount} exceeds the {authorised} authorised")
	if Decimal(str(amount)) < authorised:
		square_client.update_payment(
			settings.environment,
			token,
			payment_id,
			square_client.update_amount_body(payment_id, amount, currency),
		)
	return square_client.complete_payment(settings.environment, token, payment_id)["id"]
