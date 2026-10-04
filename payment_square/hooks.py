# SPDX-License-Identifier: AGPL-3.0-or-later
from . import __version__ as _version

app_name = "payment_square"
app_title = "Payment Square"
app_publisher = "Zephyrex Technologies Limited"
app_description = "Square payment gateway for Frappe/ERPNext Payment Requests and the webshop"
app_email = "jameson@zephyrex.ca"
app_license = "AGPL-3.0-or-later"
app_version = _version

# payments: Payment Gateway doctype and the payment-success/-failed pages; erpnext: Payment Request.
required_apps = ["frappe", "payments", "erpnext"]
