from django.core.management.base import BaseCommand, CommandError
from billing import delivery, pdf, services
from billing.models import Receipt
from billing.permissions import BillingError


class Command(BaseCommand):
    help = 'Deliver pending receipts once. Failed/unconfirmed receipts need owner review; never auto-retried.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, limit, **options):
        if not 1 <= limit <= 1000:
            raise CommandError('Limit must be between 1 and 1000.')
        try:
            delivery._enabled()
        except BillingError as exc:
            raise CommandError(str(exc))
        ids = list(Receipt.objects.filter(email_status=Receipt.EmailStatus.PENDING).order_by('pk').values_list('pk', flat=True)[:limit])
        sent = failed = 0
        for pk in ids:
            result = services.deliver_receipt(Receipt.objects.get(pk=pk), pdf.render_paid_copy, delivery.send_receipt_email)
            sent += result.email_status == Receipt.EmailStatus.SENT
            failed += result.email_status == Receipt.EmailStatus.FAILED
        self.stdout.write(f'{sent} sent; {failed} failed or unconfirmed.')
