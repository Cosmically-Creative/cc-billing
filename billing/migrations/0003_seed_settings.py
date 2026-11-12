"""Starting settings, tax rate and product types (no client data)."""

import datetime as dt

from django.db import migrations


def seed(apps, schema_editor):
    BusinessSettings = apps.get_model("billing", "BusinessSettings")
    TaxRate = apps.get_model("billing", "TaxRate")
    Product = apps.get_model("billing", "Product")
    BusinessSettings.objects.get_or_create(pk=1)
    # Iowa 6% state + 1% local option, charged since June 2026 (spec §4). Blake can add a new
    # rate with a later effective date; issued invoices keep the rate they were approved with.
    TaxRate.objects.get_or_create(
        effective_from=dt.date(2026, 6, 1),
        defaults={"name": "Iowa sales tax", "rate_bp": 700, "state_bp": 600, "local_bp": 100},
    )
    for name, kind, taxable in [
        ("Website build", "build", True),
        ("Website design", "build", True),
        ("Additional page", "build", True),
        ("Web hosting (annual)", "hosting", False),
        ("Domain registration (annual)", "domain", False),
        ("Platform renewal", "platform", False),
        ("Late fee", "late_fee", False),
    ]:
        Product.objects.get_or_create(name=name, defaults={"kind": kind, "default_taxable": taxable})


class Migration(migrations.Migration):
    dependencies = [("billing", "0002_database_guards")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
