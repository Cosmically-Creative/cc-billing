from django.core.management.base import BaseCommand

from billing.importer import import_stripe_customers


class Command(BaseCommand):
    help = "Import Stripe customers who have invoices, from the export's customers.csv (safe to re-run)."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")

    def handle(self, csv_path, **options):
        r = import_stripe_customers(csv_path)
        self.stdout.write(
            f"imported {len(r['imported'])}, already there {len(r['skipped_existing'])}, "
            f"left out (no invoices) {len(r['skipped_no_invoices'])}"
        )
