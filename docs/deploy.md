# Deploy notes (not deployed yet)

Target: Main VPS, `/opt/billing`, Docker Compose, host Caddy → `127.0.0.1:3040`,
`billing.cosmicallycreative.com`. The DNS record is added only once the app is running.

## Before the first start
- `.env` from `.env.example`, mode 600. Secrets come from the OpenBot Vault, never from chat or this repo.
- Stripe: restricted **test-mode** key first (Checkout Sessions write; Payment Intents, Charges,
  Balance Transactions and Refunds read) + webhook signing secret.
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


## Readiness checks prepared in this build

Follow [the supervised checklist](test-install-checklist.md), [Ledger interface](service-api.md)
and [Stripe transport](stripe-transport.md). These files do not authorize an installation.
`ops/Caddyfile.example` is a candidate only. The built app and default environment leave
private API, Checkout, webhooks, live mode and email delivery disabled.

CI now restores a fake-data PostgreSQL dump and media copy to new generated databases, checks
counts/audit/PDF hashes/guards/numbering, then removes only those temporary databases.
The container build also runs Django and native PDF smoke commands. This does not replace the
actual staging/offsite restore and restricted app-role checks before issuing a real invoice.
