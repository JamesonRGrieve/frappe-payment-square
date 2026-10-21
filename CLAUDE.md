# frappe-payment-square — Agent Operating Guide

AGPL-3.0-or-later Frappe v16 app: a Square Payment Gateway for ERPNext Payment Requests (invoice
links and frappe-webshop checkout). Requires `payments` and `erpnext`.

## Flow

1. ERPNext Payment Request → `SquareSettings.get_payment_url` → `/square_checkout?reference_doctype=Payment Request&reference_docname=<PR>`.
2. The page loads the Web Payments SDK (sandbox or production per `environment`), and `card.tokenize()` produces the `source_id`.
3. `api.make_payment(payment_request, source_id, gift_card_source_id)` → `SquareSettings.charge` → `checkout.charge`, which calls CreatePayment with the **Payment Request's** `grand_total`/`currency`, never client values. The idempotency key is sha256(PR name + token + amount). Every attempt gets an Integration Request (service `Square`) whose output lists every payment id and amount (frappe-square-sync skips those payments).
   - The `square_payment_order` hook (frappe-square-sync) may return a Square order for the PR's document: payments are taken against it.
   - Square gift card (`payments.giftCard()`): authorised with `accept_partial_authorization` and `autocomplete: false`. Covers it all → completed (or PayOrder). Covers part → the card pays the rest, both held then completed together; no card → the authorisation is cancelled and the buyer is asked for one. Any failure cancels every authorisation held.
4. On `COMPLETED`, `api.settle`:
   - books the gift card part first: a Payment Entry to the settings' gift card liability account and mode of payment (it spends a balance already owed);
   - runs `on_payment_authorized("Completed")` (the webshop override redirects store customers);
   - if the PR is still unpaid (stock ERPNext has no such hook; the webshop's skips guests), calls `set_as_paid()` as Administrator → Payment Entry.
5. Webhook `payment.updated` + `COMPLETED`, with `reference_id` = PR name: settle if still payable and it is a whole card payment; a gift card or split part is logged for an accountant instead (never booked wrong or twice).
6. Refund: each payment back to where it came from (gift card part to the gift card). Signature = base64(HMAC-SHA256(key, webhook_url + raw body)); `webhook_url` must match the Square subscription exactly.

## Rules

- No Square SDK dependency: `square_client.py` holds the request shapes and the two HTTP calls (30 s timeout).
- Secrets live only in `Square Settings` Password fields, written by the pipeline from OpenBao.
- **Nothing has run against the Square sandbox yet.** Never claim a live test.
- Go-live gate: a sandbox round trip end to end before any store enables checkout: a card payment and refund; a gift card alone; a gift card short of the total (asked for a card, then split); a declined card after a gift card authorisation (released); a webshop order paid onto its Square order; refunds of a split.

## Testing

`bench --site <site> run-tests --app payment_square`: unit tests plus checkout tests on the real ERPNext test DB, with Square replaced by `tests/replay.py` serving `tests/fixtures/*.json`. The fixtures are written by `python3 -m tools.gen_fixtures` and must pass `python3 -m tools.validate_fixtures` (each request and response against Square's OpenAPI document at a pinned commit, cached in `.cache/specs`).

UNVERIFIED until the sandbox run: that PayOrder accepts a partially authorised gift card payment beside a card payment for a webshop order; that Square reports a gift card payment's brand as `SQUARE_GIFT_CARD` in `card_details.card.card_brand`.
