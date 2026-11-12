"""Drone photo/video is taxable at the client's rate (Blake, spec v0.7 §4)."""

from django.db import migrations


def seed(apps, schema_editor):
    Product = apps.get_model("billing", "Product")
    Product.objects.get_or_create(name="Drone photo/video", defaults={"kind": "drone", "default_taxable": True})


class Migration(migrations.Migration):
    dependencies = [("billing", "0008_drone_kind")]
    operations = [migrations.RunPython(seed, migrations.RunPython.noop)]
