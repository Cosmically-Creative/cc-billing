"""Stripe history import. December: customers who have invoices (spec v0.6 §10).
January: the invoices themselves, read-only with their original numbers (not built yet).

Reads the export Ledger made (customers.csv: stripe_customer_id, name, tax_exempt, invoices_in_stripe, note).
The export holds real client data, so it lives only on the server, never in this repo."""

import csv

from django.db import transaction

from . import audit
from .models import Actor, Customer
from .permissions import ActorRef, RuleError, require

REQUIRED = {"stripe_customer_id", "name", "tax_exempt", "invoices_in_stripe"}


def _yes(value):
    v = (value or "").strip().lower()
    if v not in ("yes", "no", "true", "false", "1", "0", ""):
        raise RuleError(f"unexpected tax_exempt value {value!r}")
    return v in ("yes", "true", "1")


def import_stripe_customers(path, actor=None):
    """Import customers with at least one Stripe invoice. Safe to run again: existing Stripe IDs are skipped.
    Returns {"imported": [...names], "skipped_existing": [...], "skipped_no_invoices": [...]}."""
    actor = actor or ActorRef(Actor.IMPORT, "stripe-export")
    require(actor, "import", "customer")
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise RuleError(f"customers.csv is missing columns: {sorted(missing)}")
        rows = list(reader)
    result = {"imported": [], "skipped_existing": [], "skipped_no_invoices": []}
    with transaction.atomic():
        for row in rows:
            name, sid = row["name"].strip(), row["stripe_customer_id"].strip()
            if not sid.startswith("cus_"):
                raise RuleError(f"bad Stripe customer id for {name!r}")
            if int(row["invoices_in_stripe"] or 0) < 1:
                result["skipped_no_invoices"].append(name)
                continue
            if Customer.objects.filter(stripe_customer_id=sid).exists():
                result["skipped_existing"].append(name)
                continue
            c = Customer.objects.create(
                name=name,
                stripe_customer_id=sid,
                tax_exempt=_yes(row["tax_exempt"]),
                exemption_note="marked exempt in Stripe" if _yes(row["tax_exempt"]) else "",
                source="stripe_import",
            )
            audit.record(actor, "import", "customer", c.pk, after={"name": name, "stripe_customer_id": sid, "tax_exempt": c.tax_exempt})
            result["imported"].append(name)
    return result


# --- customer data step (tax locations confirmed by Blake) --------------------

DATA_REQUIRED = {"name", "stripe_customer_id", "create_if_missing", "state", "jurisdiction", "jurisdiction_kind", "tax_exempt", "checked_on", "source"}


def apply_customer_data(path, confirmed_by="blake"):
    """Apply customer facts Blake gave (tax locations, exemptions), from a CSV kept on the server.
    Matches by Stripe ID, else by exact name; creates the customer only if create_if_missing=yes.
    Each location is recorded as confirmed by Blake, with the source from the file. Safe to re-run.
    Returns a list of (name, outcome)."""
    import datetime as dt

    from . import services as svc
    from .models import TaxJurisdiction

    blake = ActorRef(Actor.BLAKE, confirmed_by)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = DATA_REQUIRED - set(reader.fieldnames or [])
        if missing:
            raise RuleError(f"customer data file is missing columns: {sorted(missing)}")
        rows = list(reader)
    out = []
    with transaction.atomic():
        for row in rows:
            name, sid = row["name"].strip(), row["stripe_customer_id"].strip()
            c = Customer.objects.filter(stripe_customer_id=sid).first() if sid else None
            c = c or Customer.objects.filter(name=name).first()
            outcome = []
            if c is None:
                if not _yes(row["create_if_missing"]):
                    out.append((name, "not found; skipped"))
                    continue
                c = svc.create_customer(blake, name=name)
                if sid:
                    Customer.objects.filter(pk=c.pk).update(stripe_customer_id=sid)
                outcome.append("created")
            if row["tax_exempt"].strip() and _yes(row["tax_exempt"]) != c.tax_exempt:
                c = svc.update_customer(blake, c, tax_exempt=_yes(row["tax_exempt"]))
                outcome.append("exemption updated")
            state = row["state"].strip().upper()
            j = None
            if state == "IA":
                j = TaxJurisdiction.objects.filter(state="IA", name=row["jurisdiction"].strip(), kind=row["jurisdiction_kind"].strip()).first()
                if j is None:
                    raise RuleError(f"{name}: unknown jurisdiction {row['jurisdiction']!r}; add its rate first")
            c.refresh_from_db()
            if c.tax_state == state and c.tax_jurisdiction_id == getattr(j, "pk", None) and not c.tax_location_pending:
                outcome.append("location already confirmed")
            else:
                checked = dt.date.fromisoformat(row["checked_on"].strip()) if row["checked_on"].strip() else None
                svc.confirm_tax_location(blake, c, state, j, checked_on=checked, source=row["source"].strip())
                outcome.append(f"location confirmed: {getattr(j, 'name', state)}")
            out.append((name, ", ".join(outcome)))
    return out
