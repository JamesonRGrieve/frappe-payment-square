# frappe-payment-square — Agent Operating Guide

AGPL-3.0-or-later Frappe v16 app: a Square Payment Gateway for ERPNext Payment Requests (invoice
links and frappe-webshop checkout). Requires `payments` and `erpnext`.

## Flow

1. ERPNext Payment Request → `SquareSettings.get_payment_url` → `/square_checkout?reference_doctype=Payment Request&reference_docname=<PR>`.
2. The page loads the Web Payments SDK (sandbox or production per `environment`), and `card.tokenize()` produces the `source_id`.
3. `api.make_payment(payment_request, source_id)` → `SquareSettings.charge`, which calls CreatePayment with the **Payment Request's** `grand_total`/`currency`, never client values. The idempotency key is sha256(PR name + token). Every attempt gets an Integration Request (service `Square`).
4. On `COMPLETED`, `api.settle`:
   - runs `on_payment_authorized("Completed")` (the webshop override redirects store customers);
   - if the PR is still unpaid (stock ERPNext has no such hook; the webshop's skips guests), calls `set_as_paid()` as Administrator → Payment Entry.
5. Webhook `payment.updated` + `COMPLETED`, with `reference_id` = PR name: settle if still payable. Signature = base64(HMAC-SHA256(key, webhook_url + raw body)); `webhook_url` must match the Square subscription exactly.

## Rules

- No Square SDK dependency: `square_client.py` holds the request shapes and the two HTTP calls (30 s timeout).
- Secrets live only in `Square Settings` Password fields, written by the pipeline from OpenBao.
- Go-live gate: a sandbox payment + refund round-trip end to end before any store enables checkout.

## Testing

`bench --site <site> run-tests --app payment_square`. These are unit tests plus DocType tests on the real test DB; the charge/refund path is proven against the Square sandbox, not mocked.
