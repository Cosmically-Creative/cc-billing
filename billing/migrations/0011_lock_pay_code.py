"""The pay code printed on the PDF is frozen with the rest of the approved document."""

import importlib

from django.db import migrations

prev = importlib.import_module("billing.migrations.0007_tax_location_guards_and_rates")
COLUMNS = prev.LOCKED_COLUMNS + ["pay_code"]
GUARD = prev.GUARD.replace(
    ", ".join(f"OLD.{c}" for c in prev.LOCKED_COLUMNS), ", ".join(f"OLD.{c}" for c in COLUMNS)
).replace(", ".join(f"NEW.{c}" for c in prev.LOCKED_COLUMNS), ", ".join(f"NEW.{c}" for c in COLUMNS))
assert "OLD.pay_code" in GUARD and "NEW.pay_code" in GUARD


class Migration(migrations.Migration):
    dependencies = [("billing", "0010_pay_code")]
    operations = [migrations.RunSQL(GUARD, prev.GUARD)]
