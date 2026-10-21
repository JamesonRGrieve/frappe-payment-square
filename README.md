# frappe-payment-square

Square payment gateway for Frappe v16 / ERPNext Payment Requests, including webshop checkout
(frappe-webshop fork stores pick their gateway account per store).

- **Square Settings**: one per Square merchant account, either `Sandbox` or `Production`. Saving it registers the Payment Gateway `Square-<gateway name>`.
- **Checkout** (`/square_checkout`): Square's Web Payments SDK tokenises the card in the browser, so card data never reaches the server. The server charges the Payment Request's own amount and then settles the request with a Payment Entry.
- **Square Gift Cards** (switch on "Accept Square Gift Cards"): the store's one gift card balance, also spendable at the Square counter. A buyer can pay with a gift card alone, or with a card for what the gift card doesn't cover (nothing is taken until the whole amount is approved). What a gift card pays is booked to the gift card liability account, never as money received.
- **Square orders**: with frappe-square-sync's webshop orders on, the payment is taken against the Square order it creates for the checkout, so Square sees the sale itself.
- **Webhook**: `/api/method/payment_square.api.webhook?gateway=<gateway name>` (`payment.updated`). Signature-checked; it settles payments the browser never reported back.
- **Refund**: `payment_square.api.refund` (Accounts Manager). The return Payment Entry stays a manual accounting step.

**Not yet run against Square.** Nothing in this app has run against the Square sandbox or a live account:
every Square request and response the tests use is recorded and validated against Square's published
OpenAPI document. The sandbox round trip in CLAUDE.md is the go-live gate.

Credentials (access token, webhook signature key) are written by the deploy pipeline from
OpenBao; never enter them by hand or commit them.

License: AGPL-3.0-or-later. Portions are adapted from frappe/payments (MIT); see NOTICE.
