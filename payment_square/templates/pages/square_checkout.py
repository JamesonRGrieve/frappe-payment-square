# SPDX-License-Identifier: AGPL-3.0-or-later
# Page shape adapted from frappe/payments' stripe_checkout (MIT, Frappe Technologies); see NOTICE.
"""Square checkout page for one Payment Request (``?reference_docname=<Payment Request>``, as sent by
``SquareSettings.get_payment_url``). Amount and currency are read from the Payment Request itself;
query-string values are display hints only and never trusted."""

import frappe
from frappe import _
from frappe.utils import fmt_money

from payment_square import square_client
from payment_square.api import payable_request

no_cache = 1


def get_context(context):
	context.no_cache = 1
	name = frappe.form_dict.get("reference_docname")
	if frappe.form_dict.get("reference_doctype") != "Payment Request" or not name:
		frappe.redirect_to_message(
			_("Payment link incomplete"),
			_("This payment link is missing information. Please ask the sender for a new one."),
		)
		frappe.local.flags.redirect_location = frappe.local.response.location
		raise frappe.Redirect
	request, settings = payable_request(name)
	context.payment_request = request.name
	context.title = frappe.form_dict.get("title") or request.company
	context.description = request.subject or ""
	context.amount = fmt_money(request.grand_total, currency=request.currency)
	context.application_id = settings.application_id
	context.location_id = settings.location_id
	context.sdk_url = square_client.WEB_SDK_URLS[settings.environment]
	context.accept_gift_cards = bool(settings.accept_gift_cards)
