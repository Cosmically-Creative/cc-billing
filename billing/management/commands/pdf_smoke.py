"""Render a bundled-template PDF without a database or filesystem writes."""
from django.core.management.base import BaseCommand,CommandError
from billing import pdf


class Command(BaseCommand):
    help='Check PDF template/font/native libraries in the built container, using sample data only.'

    def handle(self,**options):
        context=dict(kind='invoice',kind_label='INVOICE',number='SAMPLE-ONLY',
            business=dict(name='Sample business',email='sample@example.com',phone='Sample phone',address_line1='100 Example St',city_state_zip='Sample city'),
            bill_to=['Sample customer'],meta=[dict(label='Status',value='SAMPLE')],
            lines=[dict(description='Sample service',qty='1',rate='$1.00',amount='$1.00',tax_mark='–',running_subtotal='$1.00')],
            totals=dict(subtotal='$1.00',total='$1.00',show_tax=False,show_balance=True,balance_due='$1.00'),
            pay=dict(show=False),notes='Sample data only.',footer_left='Sample business · PDF smoke check',
            fonts_dir=(pdf.STATIC/'fonts').as_uri(),logo_uri=(pdf.STATIC/'cc-logo-v11.svg').as_uri())
        data=pdf.render_pdf_bytes(context)
        if not data.startswith(b'%PDF') or len(data)<1000:
            raise CommandError('PDF smoke failed.')
        self.stdout.write(f'PDF smoke passed ({len(data)} bytes); no data stored.')
