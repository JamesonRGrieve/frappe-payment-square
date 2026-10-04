# SPDX-License-Identifier: AGPL-3.0-or-later
"""Whitelisted endpoints: the checkout page's charge call, Square's webhook, and a manager-only
refund. Settling a Payment Request follows frappe/payments (MIT; see NOTICE): run the reference's
``on_payment_authorized`` hook. ERPNext's own Payment Request has no such hook and the webshop's
skips guests, so when the hook leaves the request unpaid this module records the payment itself."""

import json

import frappe
from frappe import _

from payment_square import square_client
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


def settle(request):
	"""Mark ``request`` paid after a completed Square payment; returns where to send the buyer."""
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
def make_payment(payment_request, source_id):
	"""Charge Payment Request ``payment_request`` with the Web Payments SDK token ``source_id``."""
	request, settings = payable_request(payment_request)
	payment = settings.charge(request, source_id)
	if not payment or payment.get("status") != "COMPLETED":
		frappe.db.commit()  # keep the failed Integration Request for the audit trail
		return {"redirect_to": FAILED_PAGE}
	redirect_to = settle(request)
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
			settle(frappe.get_doc("Payment Request", name))
			frappe.db.commit()
	return {"ok": True}


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
	payment_id = json.loads(log[0].output or "{}").get("payment_id") if log else None
	if not payment_id:
		frappe.throw(_("No completed Square payment found for {0}.").format(request.name))
	controller = frappe.db.get_value("Payment Gateway", request.payment_gateway, "gateway_controller")
	result = frappe.get_doc("Square Settings", controller).refund(
		payment_id, request.grand_total, request.currency, reason
	)
	frappe.get_doc("Integration Request", log[0].name).add_comment(
		"Info", f"Refund {result.get('id')}: {result.get('status')}"
	)
	return {"refund_id": result.get("id"), "status": result.get("status")}
