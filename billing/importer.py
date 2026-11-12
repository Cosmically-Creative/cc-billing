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
