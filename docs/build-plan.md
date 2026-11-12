# Build plan to the December launch (rough dates, 2026)

| When | What | Who writes it |
|---|---|---|
| Nov 13–16 | PDFs: invoice, quote, credit note, long invoices ("Page X of Y"), PAID and VOID stamps; stored at approval; visual check against Penpot | Template: worker (GLM Flash) · context/rendering/storage: Forge |
| Nov 16–21 | Screens: layout + navigation, dashboard, customers (list, record with tax location card), invoices list, quote/invoice editor with live totals (htmx), invoice detail with change-history timeline, quotes, payments ("Record check or cash") | Templates/CSS: worker · views, permissions, forms: Forge |
| Nov 21–24 | Sending: email from blake@ (SMTP via ArkHost, test mailbox only until the install), "Download PDF" + "Mark as sent", receipt delivery job; settings screen; tax report screen | Forge (email templates: worker) |
| Nov 24–28 | Ledger API (token auth, read + drafts) and the MCP layer on the private network | Forge (API docs, MCP tool wrappers: worker) |
| Nov 28–Dec 3 | Pay page (choose an amount), Stripe test mode Checkout + signed webhooks; deploy files, backups, monitors | Forge |
| ~Dec 4 | Test install on Main VPS (sample data, Stripe test mode); Stripe test key + SMTP password via the Vault | Forge, with Blake's OK |
| Dec 4–14 | Ledger's acceptance run, fixes, restore test, data step (customers + confirmed tax locations), live Stripe keys | Forge + Ledger |
| ~Dec 15 | First real invoice CC-2026-0001 | Blake |

Workers: GLM Flash only, sample data only, only the files a job needs; Forge reviews every line before it is committed.
They never touch billing math, numbering, locking, the change log, tax, permissions, Stripe, secrets or deployment.
