# frappe-payment-square

Square payment gateway for Frappe v16 / ERPNext Payment Requests, including webshop checkout
(frappe-webshop fork stores pick their gateway account per store).

- **Square Settings**: one per Square merchant account, either `Sandbox` or `Production`. Saving it registers the Payment Gateway `Square-<gateway name>`.
- **Checkout** (`/square_checkout`): Square's Web Payments SDK tokenises the card in the browser, so card data never reaches the server. The server charges the Payment Request's own amount and then settles the request with a Payment Entry.
- **Webhook**: `/api/method/payment_square.api.webhook?gateway=<gateway name>` (`payment.updated`). Signature-checked; it settles payments the browser never reported back.
- **Refund**: `payment_square.api.refund` (Accounts Manager). The return Payment Entry stays a manual accounting step.

Credentials (access token, webhook signature key) are written by the deploy pipeline from
OpenBao; never enter them by hand or commit them.

License: AGPL-3.0-or-later. Portions are adapted from frappe/payments (MIT); see NOTICE.
