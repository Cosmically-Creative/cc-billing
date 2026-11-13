"""Email transport. Disabled by default; test with Django's in-memory backend.

SMTP cannot promise exactly-once delivery after an ambiguous disconnect. Durable document-send
claims and pending-only receipt processing prevent blind automatic retries. Owner reviews failures.
"""
import hashlib
from pathlib import Path
from django.conf import settings
from django.core.mail import EmailMessage
from django.core.validators import validate_email
from django.db import transaction
from django.template.loader import render_to_string
from django.utils import timezone
from . import audit, pdf, services
from .models import Document, DocumentEmail
from .permissions import RuleError, require


def _enabled():
    if not getattr(settings, 'CCB_EMAIL_ENABLED', False):
        raise RuleError('Email delivery is disabled until the test install.')
    if settings.EMAIL_BACKEND in ('django.core.mail.backends.dummy.EmailBackend', 'django.core.mail.backends.console.EmailBackend', 'django.core.mail.backends.filebased.EmailBackend'):
        raise RuleError('Configure a delivery backend before enabling email.')


def stored_pdf(relpath, expected_hash):
    root = Path(settings.MEDIA_ROOT).resolve()
    path = (root / relpath).resolve()
    if not relpath or not path.is_relative_to(root) or not path.is_file():
        raise RuleError('The stored PDF is unavailable.')
    data = path.read_bytes()
    if not expected_hash or hashlib.sha256(data).hexdigest() != expected_hash:
        raise RuleError('The stored PDF failed its integrity check.')
    return data


def document_defaults(doc):
    pay_url = f'{pdf.base_url()}/pay/{doc.pay_code}' if doc.pay_code and not doc.pay_link_disabled and not doc.donated and services.balance_cents(doc) >= 50 else ''
    context = {'doc': doc, 'business': doc.business_snapshot, 'pay_url': pay_url}
    return dict(to=(doc.customer_snapshot or {}).get('email', ''),
                subject=doc.email_subject or f'Cosmically Creative · {doc.get_kind_display()} {doc.display_number}',
                body=doc.email_body or render_to_string('billing/email/document.txt', context))


def send_document(actor, doc, request_id, recipient, subject, body):
    require(actor, 'send', 'document', doc.pk)
    _enabled()
    validate_email(recipient)
    if not subject.strip() or '\n' in subject or '\r' in subject:
        raise RuleError('Use a single-line email subject.')
    with transaction.atomic():
        current = Document.objects.select_for_update().get(pk=doc.pk)
        previous = DocumentEmail.objects.filter(request_id=request_id).first()
        if previous:
            if previous.document_id != current.pk:
                raise RuleError('This submission belongs to another document.')
            return previous
        if current.status not in [Document.Status.APPROVED, Document.Status.SENT, Document.Status.PARTLY_PAID, Document.Status.PAID, Document.Status.ACCEPTED] or current.source != 'app':
            raise RuleError('This document cannot be emailed in its current state.')
        data = stored_pdf(current.pdf_file, current.pdf_sha256)
        message = EmailMessage(subject, body, from_email=f"Cosmically Creative <{current.business_snapshot['email']}>", to=[recipient])
        message.attach(f'{current.display_number}.pdf', data, 'application/pdf')
        # Commit the claim before SMTP. An interrupted request must not be sent automatically again.
        claim = DocumentEmail.objects.create(request_id=request_id, document=current, recipients=[recipient], subject=subject, body=body, pdf_sha256=current.pdf_sha256)
        audit.record(actor, 'email_started', 'document', current.pk, after={'email_id': claim.pk, 'to': [recipient], 'pdf_sha256': current.pdf_sha256})
    try:
        if message.send(fail_silently=False) != 1:
            raise RuntimeError('The mail backend did not accept the message.')
    except Exception:
        DocumentEmail.objects.filter(pk=claim.pk).update(status='failed', last_error='Delivery failed or is unconfirmed. Check the mailbox before sending again.')
        audit.record(actor, 'email_failed', 'document', current.pk, after={'email_id': claim.pk}, note='Delivery failed or unconfirmed; no automatic retry.')
    else:
        with transaction.atomic():
            services.mark_sent(actor, current, method='email', recipients=[recipient])
            DocumentEmail.objects.filter(pk=claim.pk).update(status='sent', sent_at=timezone.now())
            audit.record(actor, 'email_sent', 'document', current.pk, after={'email_id': claim.pk, 'to': [recipient], 'pdf_sha256': current.pdf_sha256})
    claim.refresh_from_db()
    return claim


def send_receipt_email(receipt, summary, paid_copy):
    _enabled()
    for address in summary['to']:
        validate_email(address)
    inv = receipt.payment.invoice
    summary = {**summary, 'business': inv.business_snapshot, 'url': f'{pdf.base_url()}/pay/{inv.pay_code}'}
    message = EmailMessage(f"Cosmically Creative · Receipt for {summary['invoice_number']}", render_to_string('billing/email/receipt.txt', summary),
                           from_email=f"Cosmically Creative <{summary['from']}>", to=summary['to'])
    if paid_copy:
        message.attach(f'{inv.display_number}-PAID.pdf', stored_pdf(paid_copy.pdf_file, paid_copy.pdf_sha256), 'application/pdf')
    if message.send(fail_silently=False) != 1:
        raise RuntimeError('The mail backend did not accept the receipt.')
