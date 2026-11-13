# Supervised test install checklist

Prepared only. No Main VPS install, DNS, email or Stripe account change is authorized by this file.
The owner approves the actual test install and production changes separately.

## Before installing
- Choose the exact green GitHub commit. CI must pass PostgreSQL tests, sample DB/media restore,
  container build, container Django checks and native PDF smoke. Preserve the CI run link.
- Inspect Main VPS capacity, existing Docker networks/ports and Caddy configuration read-only.
  Back up the existing configuration before approved changes. Keep a rollback image/config.
- Use a separate sample database/project/volume; never reuse or clear an existing billing database.
- Obtain a sandbox/restricted test key, webhook secret and SMTP password through the Vault.
  No credentials, real client files or tokens enter the repository or worker sandbox.
- Configure test-only recipients for email. Confirm the specific SMTP test recipient before sending.

## Network and application checks
- Add the candidate Caddy route only with approval. Public `/api*`, `/mcp*`, `/admin*` must be 404.
- Bind the app's host port to loopback. Connect the private OpenBot network only after verifying it.
  Set `CCB_SERVICE_NETWORKS` to OpenBot's actual private source address, not a broad/Caddy network.
- Create Ledger's token into a new private mode-600 staging file, transfer it through secure
  configuration, then remove the staging file. Verify revoked/read-only tokens are refused correctly.
- Verify password + authenticator sign-in. Check that password-only sessions cannot access owner UI.
- Check migrations, runtime health, embedded fonts, original PDF hash validation and media permissions.
- Confirm logs omit capability URLs, query strings, Authorization headers, provider keys and codes.

## Payment/email acceptance with sample data
- Use the selected Stripe sandbox, test mode only; register the exact webhook events from stripe-transport.md.
- Run full/partial/minimum/below-minimum/balance-cap cases; prove a return URL never changes money.
- Test card success, delayed bank success/failure, duplicate events, a stale/invalid signature,
  two browser tabs, and a manual payment while a checkout reservation is active.
- Confirm actual Stripe fees, paid status, one receipt record, partial receipt without PDF, final
  receipt with the separate PAID copy, no original-file change and no duplicate provider receipts.
- Test refund before/after payment-event delivery; owner decides reopen or reviews a matching credit
  draft. Confirm original tax/exemption and no refund email. Never auto-forgive a refund.
- Send only the approved sample SMTP message, then verify sender, recipient, attachment and actual
  delivery. Simulate a failure and inspect the mailbox before an intentional resend.
- Configure pending-receipt delivery and monitoring through OpenBot's visible routines at install;
  do not add a hidden cron/provider schedule. Interrupted sends/checkouts need operator reconciliation.

## Backup and restore before any real invoice
- Add DB dumps and referenced original/PAID PDFs to the approved encrypted offsite backup.
  Keep deployment secrets outside the repository; validate that the backup can restore the app.
- Restore a real staging backup into a new isolated database and media directory. Compare row counts,
  verify `audit.verify_chain()`, every stored PDF hash, append-only/locking guards and next invoice number.
- The CI smoke uses only fake data; it does not verify offsite access, encryption, full VPS recovery or
  the production database role. Those checks remain mandatory here.
- The runtime DB role must not have UPDATE/DELETE/TRUNCATE on append-only tables; migrations use a
  separate owner role. Confirm restored app-role privileges, not only superuser access.

## Before live cutover (separate approval)
- Use a fresh live database/volume after sample acceptance; keep the sample environment isolated.
  Load approved customer data from the external import/data file, not repo fixtures.
- Obtain live restricted credentials through the Vault, validate live/test separation and enable
  the explicit live-mode gates only with approval. Do not change the live account through a sandbox run.
- Owner reviews Stripe's receipt settings to prevent duplicate client receipts. No setting is changed here.
- Confirm offsite restore, alerts, original/PAID archive and the first real document before issuing it.
- Current targets remain approximately Dec 4 test install / Dec 15 first invoice; acceptance determines readiness.
