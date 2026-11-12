"""1) The document lock also covers the new tax fields.
2) Starting rates: Iowa 6% state, and the local option jurisdictions Ledger checked on 2026-11-12
   against the Iowa DOR rate map (all 1%, no end date). Le Mars (1%, ending in 2032) is left for
   Ledger's monthly rate check, which records its exact end date. Public tax data, no client data."""

import datetime as dt

from django.db import migrations

LOCKED_COLUMNS = [
    "kind", "customer_id", "series", "year", "seq", "number", "issue_date", "due_date", "donated",
    "late_fee_enabled", "late_fee_bp", "notes", "customer_snapshot", "business_snapshot",
    "tax_override_state", "tax_override_jurisdiction_id", "tax_state", "tax_jurisdiction_id",
    "out_of_state", "tax_label", "tax_rate_bp", "state_rate_bp", "local_rate_bp",
    "subtotal_cents", "discount_cents", "taxable_cents", "tax_cents", "state_tax_cents", "local_tax_cents",
    "value_cents", "total_cents", "pdf_file", "pdf_sha256", "prepared_by", "source", "original_number",
    "stripe_invoice_id", "from_quote_id", "replaces_id", "credit_for_id", "late_fee_for_id",
    "late_fee_period", "approved_at",
]  # fmt: skip

_old = ", ".join(f"OLD.{c}" for c in LOCKED_COLUMNS)
_new = ", ".join(f"NEW.{c}" for c in LOCKED_COLUMNS)

GUARD = f"""
CREATE OR REPLACE FUNCTION ccb_document_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'document % (%) is not a draft and cannot be deleted', OLD.id, OLD.number
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.status <> 'draft' THEN
    IF ROW({_old}) IS DISTINCT FROM ROW({_new}) THEN
      RAISE EXCEPTION 'document % (%) is locked; void it or issue a credit note', OLD.id, OLD.number
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'draft' THEN
      RAISE EXCEPTION 'document % cannot go back to draft', OLD.number USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'void' AND NEW.status <> 'void' THEN
      RAISE EXCEPTION 'document % is void; void is final', OLD.number USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;
"""

APP_START = dt.date(2026, 1, 1)
CHECKED = dt.date(2026, 11, 12)
SOURCE = "Iowa DOR sales tax rate map, checked by Ledger"

JURISDICTIONS = [
    ("Sioux Center", "city", "Sioux"),
    ("Orange City", "city", "Sioux"),
    ("Hull", "city", "Sioux"),
    ("Rock Valley", "city", "Sioux"),
    ("Sioux County", "rural_county", "Sioux"),
    ("Onawa", "city", "Monona"),
    ("Monona County", "rural_county", "Monona"),
    ("Sioux City", "city", "Woodbury"),
]


def seed(apps, schema_editor):
    StateTaxRate = apps.get_model("billing", "StateTaxRate")
    TaxJurisdiction = apps.get_model("billing", "TaxJurisdiction")
    LocalTaxRate = apps.get_model("billing", "LocalTaxRate")
    StateTaxRate.objects.get_or_create(state="IA", effective_from=APP_START, defaults={"rate_bp": 600})
    for name, kind, county in JURISDICTIONS:
        j, _ = TaxJurisdiction.objects.get_or_create(state="IA", name=name, kind=kind, defaults={"county": county})
        LocalTaxRate.objects.get_or_create(
            jurisdiction=j, effective_from=APP_START, defaults={"rate_bp": 100, "checked_on": CHECKED, "source": SOURCE}
        )


class Migration(migrations.Migration):
    dependencies = [("billing", "0006_tax_locations")]
    operations = [
        migrations.RunSQL(GUARD, migrations.RunSQL.noop),
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]
