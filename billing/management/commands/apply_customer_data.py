from django.core.management.base import BaseCommand

from billing.importer import apply_customer_data


class Command(BaseCommand):
    help = "Apply customer facts Blake confirmed (tax locations, exemptions) from a server-side CSV. Safe to re-run."

    def add_arguments(self, parser):
        parser.add_argument("csv_path")

    def handle(self, csv_path, **options):
        for name, outcome in apply_customer_data(csv_path):
            self.stdout.write(f"- {name}: {outcome}")
