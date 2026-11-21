# SPDX-License-Identifier: AGPL-3.0-or-later
"""Square hold page (``?request=<Integration Request>``, from ``SquareSettings.get_hold_url``): the buyer's
card is authorised for the request's amount, which is read from the request itself, never the query."""

import frappe
from frappe import _
from frappe.utils import fmt_money

from payment_square import holds, square_client

no_cache = 1


def get_context(context):
	context.no_cache = 1
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = (
			f"/login?redirect-to=/square_hold?request={frappe.form_dict.request}"
		)
		raise frappe.Redirect
	log, data, settings = holds.pending(frappe.form_dict.get("request"), frappe.session.user)
	context.title = data.get("title") or _("Authorise your card")
	context.request = log.name
	context.waiting = log.status == "Queued"
	context.description = data.get("description") or ""
	context.amount = fmt_money(data["amount"], currency=data["currency"])
	context.return_url = data["return_url"]
	context.application_id = settings.application_id
	context.location_id = settings.location_id
	context.sdk_url = square_client.WEB_SDK_URLS[settings.environment]
