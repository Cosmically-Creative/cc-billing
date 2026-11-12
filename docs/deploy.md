# Deploy notes (not deployed yet)

Target: Main VPS, `/opt/billing`, Docker Compose, host Caddy → `127.0.0.1:3040`,
`billing.cosmicallycreative.com`. The DNS record is added only once the app is running.

## Before the first start
- `.env` from `.env.example`, mode 600. Secrets come from the OpenBot Vault, never from chat or this repo.
- Stripe: restricted **test-mode** key first (Checkout Sessions write; Payment Intents, Charges,
  Balance Transactions read) + webhook signing secret.
- Email: SMTP via `postal.arkhost.com` as blake@cosmicallycreative.com (SPF/DMARC on the domain only
  allow that server).

## Database roles
Run migrations as the owner role. The app runs as a separate role that has
`INSERT, SELECT` only on `billing_auditevent`, `billing_payment`, `billing_customercredit`
(no `UPDATE`, `DELETE`, `TRUNCATE`). The triggers in migration 0002 are the second layer.

## Caddy
Public: sign-in and app screens, `/pay/*`, `/stripe/webhook`, `/healthz`.
Not routed from the internet: `/api/*`, `/mcp`, `/admin/`. Ledger reaches the API over `openbot-net`.

## Backups
Add to the nightly `mainvps-backup` job: `pg_dump -Fc` before upload, `/opt/billing` (config + data),
`--keep-yearly 7`, and restore the dump in the Sunday test (row counts + `verify_chain`).

## Before going live
Wipe test data and counters, switch to live Stripe keys, run the acceptance suite, one full restore test.
