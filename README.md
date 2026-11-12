# cc-billing

Quoting and invoicing for Cosmically Creative: quotes → invoices → credit notes, Iowa sales tax,
check/cash and Stripe payments, and a permanent change history. Private; all rights reserved.

Stack: Python 3.13 · Django 5.2 LTS · PostgreSQL 18 · (WeasyPrint for PDFs, coming) · Docker Compose.

## Billing rules the code enforces

| Rule | Where |
|---|---|
| Money is integer cents, rates are basis points (700 = 7%), quantities are thousandths. Floats are rejected. | `billing/money.py` |
| Tax per taxable line, after its discount, rounded half-up, then summed. Exempt customers and donated work pay none. | `money.compute_totals` |
| Numbers (`Q-`, `CC-`, `CN-` + Central-time year + 4 digits) are assigned only at approval, from a locked counter in the same transaction. Failures roll the number back: no gaps. | `services.approve`, `_next_number` |
| Approved documents are locked. Corrections go through void + replacement or a credit note. | `services.py` + database triggers |
| Only drafts can be deleted; void is final; nothing returns to draft. | migration `0002_database_guards` |
| Payments, customer credits and the change history are append-only. History entries are hash-chained; `audit.verify_chain()` detects tampering. | `billing/audit.py`, migration `0002` |
| Online payments are recorded only from verified Stripe events, once each. Check/cash only by Blake. Overpayments become customer credit. | `services.record_stripe_payment`, `record_manual_payment` |
| Refunds are recorded from Stripe and wait for Blake's choice: credit note or reopened balance. | `services.resolve_refund` |
| Ledger's token: read + drafts only. Every refused attempt is logged. | `billing/permissions.py` |
| Sign-in: password + authenticator app (TOTP), codes can't be reused, attempts are throttled. | `accounts/` |

Business rules come from the billing spec kept in OpenBot's shared project folder (not in this repo,
because it mentions real clients). Tests use fake data only.

## Run the tests

```sh
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt
# needs a PostgreSQL 18 server; the triggers and row locks are part of what's tested
export DJANGO_DEBUG=1 POSTGRES_HOST=localhost POSTGRES_USER=... POSTGRES_PASSWORD=... POSTGRES_DB=ccbilling
pytest -v
```

CI runs the same suite against PostgreSQL 18 on every push and pull request.

## Run locally with Docker

```sh
cp .env.example .env   # fill in real values; never commit .env
docker compose up --build
docker compose exec app python manage.py create_owner blake
```

Then open http://127.0.0.1:3040/account/login/. The first sign-in shows a QR code for the authenticator app.

## Status

Built so far: data model, billing rules, database guards, change history, sign-in, Ledger token model.
Next: screens from Prism's Penpot design, PDFs, email sending, the Ledger API + MCP layer, Stripe.
