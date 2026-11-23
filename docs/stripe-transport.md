# Hosted Checkout and webhook transport

Implemented and tested locally with Stripe Python 16.1 typed resource objects and real webhook
HMAC verification. HTTP calls in the tests use a local fake; no Stripe account was changed and
no real test-mode Checkout session has been created by this build.

Configure a separate sandbox/restricted test key at the supervised install. Checkout Sessions write,
Payment Intents/Charges/Balance Transactions/Refunds read are needed. Keep the key and webhook signing
secret in the Vault and deployment-only environment; never in this repository. Public mode remains
off (`CCB_STRIPE_ENABLED=0`, `CCB_STRIPE_ACCEPT_WEBHOOKS=0`). Test keys cannot run in live mode and vice
versa. Live mode also requires the explicit `CCB_STRIPE_LIVE_ENABLED` gate, left off here.
API version: `2026-10-28.endive`; client-scoped credentials, no module-level API key.

The pay link is an unguessable invoice capability, not an API token. Responses are not cached or
indexed, send no referrer to other sites (`same-origin`; `no-referrer` makes browsers send `Origin: null` and fails CSRF), and have a restrictive CSP. It displays the current ledger balance and
can download the integrity-checked original PDF. Owner screens, email addresses and history are not
public. Pay-link-disabled invoices return 404. Redact capability paths from production access logs.

The browser chooses full balance or another dollar amount; the server parses it to cents and applies
the current configured minimum and balance cap. Under the minimum, only the full balance is accepted;
under Stripe's USD floor the page asks the client to contact the business. An unsent/void/donated/paid
invoice cannot start Checkout. Stripe handles card/bank details on its hosted page.

One active reservation per invoice prevents multiple online checkouts. Manual payments, voids and
credit-note approval are blocked until the checkout is closed or the bank payment is confirmed.
Sessions use a stable idempotency key and stored integration identifier, with no Stripe tax calculation,
extra discounts or extra fees added to the already-taxed invoice amount. Dynamic payment methods are
used; no hardcoded payment-method allowlist. Configure enabled methods in the chosen sandbox at install.
A returned/cancelled URL never changes a payment or releases a reservation. Closing requires Stripe
confirmation of expiration. Ambiguous creation retains the reservation and the same retry key;
after 15 minutes it requires operator reconciliation rather than creating another session blindly.

Required signed endpoint: `POST /stripe/webhook`. Register only the selected sandbox's events:
- `checkout.session.completed` (records only if paid; otherwise processing)
- `checkout.session.async_payment_succeeded` / `checkout.session.async_payment_failed`
- `checkout.session.expired`
- `refund.created`, `refund.updated`, `charge.refunded`

Verified mode, local attempt, session ID, invoice metadata, USD amount, PaymentIntent and actual fee
must match before a payment is recorded. Fees/lookup temporarily unavailable returns 503 for retry;
no partial payment/receipt state is stored. Event IDs and payment intent IDs each prevent duplicates.
Only necessary event identifiers are retained, not raw card/billing payloads. For charge refunds, all
refund pages are reconciled, not only the first embedded page. Refunds arriving before their payment
return 503 and retry. Refunds stay pending owner resolution; no refund email or silent balance decision. The owner can reopen the balance, or prepare and review a linked credit draft for the refunded work and approve it as part of resolution. The gross credit must match the refund; tax rates and exemption use the original invoice, even if the customer changes later.
Receipt records are queued exactly once and delivered by the existing pending-receipt command after
transport configuration. Stripe's own receipts still need owner review/disablement at launch.

Operator recovery at install: inspect `creating`/`processing` reservations before clearing them;
confirm the actual Session/PaymentIntent in Stripe, and repair through a reviewed operation with an
audit entry. Never clear a reservation merely because the browser returned or a timeout elapsed.

References:
- https://docs.stripe.com/checkout/fulfillment
- https://docs.stripe.com/webhooks
- https://docs.stripe.com/api/checkout/sessions/create
