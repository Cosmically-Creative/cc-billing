# PDF, screens and sending review — 2026-11-13

This phase was implemented and validated locally. Commit/CI status is tracked in the channel and build plan; it has not been deployed.
No billing installation, DNS, Stripe or SMTP configuration was changed.

Implemented:
- Owner UI behind password + authenticator: dashboard, customer list/contact form and confirmed tax-location card, quote/invoice lists, editor, detail and immutable change history, payments, settings, tax report with date range/CSV. Recurring is a read-only January preview.
- Editor line add/remove, discounts, product defaults, server-calculated live totals, draft PDF preview without saving or numbering, separate save/review/approve actions. Integer money math stays in existing services. Fetch is used for live totals, without an extra browser dependency.
- US Letter PDFs using bundled Rubik and Logo v11: invoice, quote/acceptance instructions, negative credit note and original-invoice link, PAID and VOID copies, long tables with repeated column headers, short later-page header, actual cumulative net subtotal on non-final pages, Page X of Y. Totals/check/payment sections stay together at the end. Grayscale credit note inspected.
- Approval saves an immutable PDF and hash. Downloads check that hash, and cannot access files outside media. PDF assets cannot fetch network URLs. A failed approval can leave an unreferenced PDF file; unique names allow the same invoice number to be retried safely. Backup/storage maintenance must preserve referenced originals and only garbage-collect unreferenced files after a grace period.
- Owner compose/review page, stored original PDF attachment, manual mark-sent action. Durable submission IDs prevent a repeated form submission from emailing twice. History records recipients, time and PDF hash.
- Pending-receipt delivery command with real PDF integration, partial receipts without attachment, paid-in-full receipts with stored PAID copy, failure reporting and owner resend.
- SMTP is disabled by default. Dummy/console/file backends cannot mark email delivered. No external mail was sent. Tests use an in-memory mailbox.

Transport limitation: SMTP cannot guarantee exactly-once delivery after an ambiguous disconnect. Both send flows record a claim before transport; interrupted sends remain unconfirmed, and failures are never automatically retried. The owner must check the mailbox before an intentional resend. An interrupted receipt in `sending` requires an operator-reviewed recovery; normal failed receipts have an owner resend button.

Checks:
- 141 passing PostgreSQL 18 tests, including real WeasyPrint PDFs and an in-memory mailbox; one skipped test covers the January Stripe history import.
- Number/date/amount rules, locks and existing acceptance tests retained.
- Browser layout checks at desktop width and approximately 390px; invoice-detail overflow fixed. These were rendered static pages from the actual templates, not a deployed end-to-end browser run.
- Django system check, migration consistency and diff whitespace check.
- Docker CLI is unavailable in this execution environment: the container image build was not run. Live SMTP, Stripe Checkout, backups/restore and VPS installation remain install/integration checks.

Workers: GLM Flash only, sample data only. Reviewed prior PDF (45,867 input / 5,188 output tokens) and app shell (8,565 / 2,617). New dashboard/list job: 19,664 / 2,022. Rejected no out-of-scope files; corrected column widths, pagination, stamps, missing fonts, white sidebar, responsive layout, accessible search label, and duplicate headings. All services, permissions, forms, delivery and arithmetic integration were written/reviewed by Forge.

Remaining launch phases: Ledger API/private MCP integration; public pay page and Stripe test-mode Checkout/webhook transport; deployment/backup/monitor preparation; supervised test install and acceptance/restore test. Live email and Stripe credentials are obtained through the Vault at the install. Existing targets remain approximately Dec 4 test install and Dec 15 first invoice; they are targets, not acceptance results.
