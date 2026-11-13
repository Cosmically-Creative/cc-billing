"""Owner workflows using fake customers, real PostgreSQL/PDFs, and an in-memory mailbox."""
import datetime as dt
import uuid
from pathlib import Path

import pymupdf
import pytest
from django.contrib.auth import get_user_model
from django.core import mail
from django.core.management import call_command
from django.test import Client
from django.urls import reverse

from billing import delivery, pdf, services as svc
from billing.models import AuditEvent, Document, DocumentEmail, NumberCounter, Payment, Receipt
from billing.permissions import PermissionDenied, RuleError
from .conftest import central, line

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def local_media(settings, tmp_path):
    settings.MEDIA_ROOT = tmp_path
    settings.EMAIL_BACKEND = 'django.core.mail.backends.locmem.EmailBackend'
    settings.CCB_EMAIL_ENABLED = False


@pytest.fixture
def owner_client(client):
    user = get_user_model().objects.create_user('sample-owner', is_superuser=True, is_staff=True)
    client.force_login(user)
    session = client.session
    session['ccb_2fa_verified'] = True
    session.save()
    return client


@pytest.fixture
def issued(blake, customer, products):
    def make(count=1, kind='invoice', **kw):
        doc = svc.create_draft(blake, kind, customer, [line(products['build'], 10000, description=f'Sample service {i+1:02} — example work') for i in range(count)], **kw)
        return svc.approve(blake, doc, now=central(2026,12,10), render_pdf=pdf.render_approved)
    return make


def draft_data(customer):
    return {'customer':customer.pk, 'notes':'Sample notes', 'lines-TOTAL_FORMS':'1', 'lines-INITIAL_FORMS':'0', 'lines-MIN_NUM_FORMS':'1', 'lines-MAX_NUM_FORMS':'200',
            'lines-0-description':'Sample work', 'lines-0-quantity':'1', 'lines-0-rate':'100.00', 'lines-0-discount':'0', 'lines-0-taxable':'on'}


@pytest.mark.parametrize('url', ['/', '/customers/', '/documents/invoice/', '/documents/quote/', '/payments/', '/settings/', '/reports/tax/', '/recurring/'])
def test_pages_require_owner_and_two_factor(client, url):
    assert client.get(url).status_code == 302
    user = get_user_model().objects.create_user('sample-reader')
    client.force_login(user)
    session = client.session; session['ccb_2fa_verified'] = True; session.save()
    assert client.get(url).status_code == 403
    user.is_superuser = True; user.save()
    session = client.session; session.pop('ccb_2fa_verified'); session.save()
    assert client.get(url).status_code == 302


@pytest.mark.parametrize('url', ['/', '/customers/', '/documents/invoice/', '/documents/quote/', '/payments/', '/settings/', '/reports/tax/', '/recurring/', '/documents/invoice/new/', '/payments/new/'])
def test_screens_render_with_records(owner_client, issued, url):
    issued()
    response = owner_client.get(url)
    assert response.status_code == 200
    assert b'no-store' in response['Cache-Control'].encode()
    assert b'SAMPLE DATA' not in response.content  # never hardcode the design's fixture rows


def test_customer_data_and_location_forms(owner_client):
    response = owner_client.post('/customers/new/', {'name':'Sample Studio', 'email':'sample@example.com'})
    assert response.status_code == 302
    response = owner_client.get(response.url)
    assert b'Confirm or change location' in response.content
    assert b'Sample Studio' in response.content


def test_editor_preview_never_saves_or_takes_number(owner_client, customer):
    data = draft_data(customer)
    data['action'] = 'preview'
    response = owner_client.post('/documents/invoice/new/', data)
    assert response.status_code == 200 and b'$107.00' in response.content
    assert Document.objects.count() == 0 and NumberCounter.objects.count() == 0
    data['action'] = 'preview_pdf'
    response = owner_client.post('/documents/invoice/new/', data)
    assert response['Content-Type'] == 'application/pdf'
    pages = pymupdf.open(stream=response.content, filetype='pdf')
    assert 'DRAFT' in pages[0].get_text()
    assert Document.objects.count() == 0 and NumberCounter.objects.count() == 0


def test_save_edit_approve_download_and_mark_sent(owner_client, customer):
    data = draft_data(customer)
    response = owner_client.post('/documents/invoice/new/', data)
    doc = Document.objects.get()
    assert response.url == reverse('billing:document', args=[doc.pk]) and doc.number == ''
    assert b'Sample work' in owner_client.get(response.url).content
    data['notes'] = 'Changed note'
    data['lines-INITIAL_FORMS'] = '1'
    assert owner_client.post(reverse('billing:edit_document', args=[doc.pk]), data).status_code == 302
    url = reverse('billing:document_action', args=[doc.pk])
    assert owner_client.get(url).status_code == 405
    assert owner_client.post(url, {'action':'approve'}).status_code == 302
    doc.refresh_from_db()
    assert doc.number and doc.pdf_file
    original = Path(doc.pdf_file).name
    download = owner_client.get(reverse('billing:pdf', args=[doc.pk]))
    assert download.content.startswith(b'%PDF')
    doc.refresh_from_db(); assert doc.status == 'approved' and doc.sent_at is None
    assert owner_client.post(reverse('billing:edit_document', args=[doc.pk]), data).status_code == 409
    owner_client.post(url, {'action':'mark_sent'})
    doc.refresh_from_db(); assert doc.status == 'sent'
    assert Path(doc.pdf_file).name == original
    assert AuditEvent.objects.filter(action='sent_manually', entity_id=str(doc.pk)).exists()


def test_csrf_rejects_state_change(owner_client, issued):
    doc = issued()
    strict = Client(enforce_csrf_checks=True)
    strict.cookies = owner_client.cookies
    assert strict.post(reverse('billing:document_action', args=[doc.pk]), {'action':'mark_sent'}).status_code == 403


def test_invalid_quantity_discount_and_missing_location(owner_client, customer):
    data = draft_data(customer)
    data['lines-0-quantity'] = 'NaN'
    assert owner_client.post('/documents/invoice/new/', data).status_code == 200
    assert Document.objects.count() == 0
    data['lines-0-quantity'] = '1'
    data['lines-0-discount'] = '101'
    assert owner_client.post('/documents/invoice/new/', data).status_code == 200
    assert Document.objects.count() == 0


def test_17_invoice_send_records_and_deduplicates(blake, ledger, issued, settings):
    doc = issued()
    with pytest.raises(RuleError):
        delivery.send_document(blake, doc, uuid.uuid4(), 'sample@example.com', 'Invoice', 'Attached')
    settings.CCB_EMAIL_ENABLED = True
    with pytest.raises(PermissionDenied):
        delivery.send_document(ledger, doc, uuid.uuid4(), 'sample@example.com', 'Invoice', 'Attached')
    key = uuid.uuid4()
    result = delivery.send_document(blake, doc, key, 'sample@example.com', 'Sample invoice', 'Attached sample')
    again = delivery.send_document(blake, doc, key, 'sample@example.com', 'Sample invoice', 'Attached sample')
    assert result.pk == again.pk and result.status == 'sent'
    assert len(mail.outbox) == 1
    msg = mail.outbox[0]
    assert msg.from_email == 'Cosmically Creative <blake@cosmicallycreative.com>' and msg.to == ['sample@example.com']
    assert len(msg.attachments) == 1 and msg.attachments[0].content.startswith(b'%PDF')
    assert result.pdf_sha256 == doc.pdf_sha256
    doc.refresh_from_db(); assert doc.sent_method == 'email' and doc.status == 'sent'
    assert AuditEvent.objects.filter(action='email_sent', entity_id=str(doc.pk), after__pdf_sha256=doc.pdf_sha256).exists()


def test_send_failure_and_unconfirmed_claim_not_auto_retried(blake, issued, settings, monkeypatch):
    doc = issued()
    settings.CCB_EMAIL_ENABLED = True
    calls = []
    def fail(*a, **kw):
        calls.append(1)
        raise ConnectionError('sample SMTP down')
    monkeypatch.setattr(delivery.EmailMessage, 'send', fail)
    key = uuid.uuid4()
    result = delivery.send_document(blake, doc, key, 'sample@example.com', 'Sample', 'Sample')
    assert result.status == 'failed'
    delivery.send_document(blake, doc, key, 'sample@example.com', 'Sample', 'Sample')
    assert len(calls) == 1
    doc.refresh_from_db(); assert doc.status == 'approved'
    DocumentEmail.objects.filter(pk=result.pk).update(status='sending')
    assert delivery.send_document(blake, doc, key, 'sample@example.com', 'Sample', 'Sample').status == 'sending'
    assert len(calls) == 1


def test_send_form_disabled_and_escape(owner_client, issued):
    doc = issued()
    response = owner_client.get(reverse('billing:send', args=[doc.pk]))
    assert response.status_code == 200 and b'Email it' in response.content
    response = owner_client.post(reverse('billing:send', args=[doc.pk]), {'request_id':str(uuid.uuid4()), 'to':'sample@example.com','subject':'Sample','body':'Hello'})
    assert b'disabled until the test install' in response.content
    assert not DocumentEmail.objects.exists()


def test_real_receipts_partial_final_and_once(blake, issued, settings):
    settings.CCB_EMAIL_ENABLED = True
    doc = issued()
    original = delivery.stored_pdf(doc.pdf_file, doc.pdf_sha256)
    svc.mark_sent(blake, doc)
    svc.record_manual_payment(blake, doc, 'cash', 4000, dt.date(2026,12,12))
    svc.record_manual_payment(blake, doc, 'check', 6700, dt.date(2026,12,13), check_number='1042')
    call_command('deliver_receipts')
    call_command('deliver_receipts')
    assert len(mail.outbox) == 2
    partial, final = mail.outbox
    assert not partial.attachments and '$67.00' in partial.body and '$40.00' in partial.body
    assert 'Paid in full' in final.body and 'Check #1042' in final.body
    assert len(final.attachments) == 1
    paid_pdf = pymupdf.open(stream=final.attachments[0].content, filetype='pdf')
    text = '\n'.join(page.get_text() for page in paid_pdf)
    assert 'PAID' in text and '$0.00' in text and '$40.00' in text and '$67.00' in text
    assert delivery.stored_pdf(doc.pdf_file, doc.pdf_sha256) == original
    assert Receipt.objects.filter(email_status='sent').count() == 2


def test_interrupted_receipt_is_not_resent(blake, issued):
    doc = issued()
    payment = svc.record_manual_payment(blake, doc, 'cash', 4000, dt.date(2026,12,12))
    def interrupted(*args):
        raise KeyboardInterrupt('simulate process interruption')
    with pytest.raises(KeyboardInterrupt):
        svc.deliver_receipt(payment.receipt, pdf.render_paid_copy, interrupted)
    payment.receipt.refresh_from_db()
    assert payment.receipt.email_status == 'sending'
    def must_not_send(*a):
        pytest.fail('Interrupted receipt must not automatically send again')
    svc.deliver_receipt(payment.receipt, pdf.render_paid_copy, must_not_send)


def test_download_integrity_and_path_safety(issued, settings):
    doc = issued()
    Path(settings.MEDIA_ROOT, doc.pdf_file).write_bytes(b'tampered')
    with pytest.raises(RuleError):
        delivery.stored_pdf(doc.pdf_file, doc.pdf_sha256)
    with pytest.raises(RuleError):
        delivery.stored_pdf('../outside.pdf', 'x')
    with pytest.raises(ValueError):
        pdf.asset_fetcher('https://example.com/logo.svg')


def test_rendered_long_pdf_headers_subtotals_and_no_clipping(issued):
    doc = issued(count=32)
    data = delivery.stored_pdf(doc.pdf_file, doc.pdf_sha256)
    rendered = pymupdf.open(stream=data, filetype='pdf')
    assert len(rendered) >= 2
    text = [page.get_text() for page in rendered]
    for index, page in enumerate(rendered, 1):
        assert page.rect.width == 612 and page.rect.height == 792
        assert f'Page {index} of {len(rendered)}' in text[index-1]
        if 'Sample service' in text[index-1]:
            assert 'DESCRIPTION' in text[index-1]
        for word in page.get_text('words'):
            assert 0 <= word[0] < word[2] <= 612.5 and 0 <= word[1] < word[3] <= 792.5, word
    assert 'Subtotal so far' in text[0]
    assert 'continued' in text[1]
    assert all('Continued on page 2' not in t for t in text)
    assert all('BALANCE DUE' not in t and 'Pay online' not in t for t in text[:-1])
    assert 'BALANCE DUE' in text[-1] and 'Pay online' in text[-1]
    for i in range(32):
        assert sum(f'Sample service {i+1:02}' in t for t in text) == 1


def test_quote_pdf_has_acceptance_without_pay_link(issued):
    doc = issued(kind='quote')
    rendered = pymupdf.open(stream=delivery.stored_pdf(doc.pdf_file, doc.pdf_sha256), filetype='pdf')
    text = '\n'.join(p.get_text() for p in rendered)
    assert 'Accept this quote' in text and 'Pay online' not in text and 'BALANCE DUE' not in text


def test_tax_report_and_payment_form(owner_client, issued):
    doc = issued()
    response = owner_client.post('/payments/new/', {'invoice':doc.pk, 'method':'check', 'amount':'40.00', 'received_on':'2026-12-12', 'check_number':'1042', 'email_receipt':'on'})
    assert response.status_code == 302 and Payment.objects.count() == 1
    assert b'Check #1042' in owner_client.get(reverse('billing:document', args=[doc.pk])).content
    response = owner_client.get('/reports/tax/?start=2026-12-01&end=2026-12-31')
    assert response.status_code == 200 and b'$7.00' in response.content
    response = owner_client.get('/reports/tax/?start=2026-12-01&end=2026-12-31&format=csv')
    assert response.status_code == 200 and b'Sioux Center,$100.00,$6.00,$1.00' in response.content
    response = owner_client.get('/reports/tax/?start=2026-12-31&end=2026-12-01')
    assert b'end date must be on or after' in response.content


def test_credit_editor_negative_pdf_and_void_copy(owner_client, issued, blake):
    doc = issued()
    response = owner_client.get(reverse('billing:new_credit', args=[doc.pk]))
    assert response.status_code == 200
    data = draft_data(doc.customer)
    response = owner_client.post(reverse('billing:new_credit', args=[doc.pk]), data)
    credit = Document.objects.get(kind='credit_note')
    assert response.url == reverse('billing:document', args=[credit.pk])
    owner_client.post(reverse('billing:document_action', args=[credit.pk]), {'action':'approve'})
    credit.refresh_from_db()
    rendered = pymupdf.open(stream=delivery.stored_pdf(credit.pdf_file, credit.pdf_sha256), filetype='pdf')
    text = '\n'.join(p.get_text() for p in rendered)
    assert 'CREDIT NOTE' in text and '-$107.00' in text and doc.number in text
    other = issued()
    original = delivery.stored_pdf(other.pdf_file, other.pdf_sha256)
    svc.void(blake, other, 'Sample correction')
    response = owner_client.get(reverse('billing:void_pdf', args=[other.pk]))
    assert response.status_code == 200
    stamped = pymupdf.open(stream=response.content, filetype='pdf')
    text = '\n'.join(p.get_text() for p in stamped)
    assert 'VOID' in text and '$0.00' in text and 'Pay online' not in text
    assert delivery.stored_pdf(other.pdf_file, other.pdf_sha256) == original


def test_settings_accepts_dollars_and_percentages(owner_client):
    from billing.models import BusinessSettings
    response = owner_client.get('/settings/')
    assert b'value="$1.00"' in response.content
    st = BusinessSettings.load()
    data = {name:getattr(st,name) for name in ['name','address_line1','city_state_zip','phone','email','send_warning_days']}
    data.update(min_online_payment_cents='5.00', late_fee_default_bp='2.5')
    assert owner_client.post('/settings/', data).status_code == 302
    st.refresh_from_db()
    assert st.min_online_payment_cents == 500 and st.late_fee_default_bp == 250
    data['min_online_payment_cents'] = '0.49'
    assert owner_client.post('/settings/', data).status_code == 200
    st.refresh_from_db(); assert st.min_online_payment_cents == 500


def test_dummy_mail_backend_cannot_mark_email_sent(blake, issued, settings):
    settings.CCB_EMAIL_ENABLED = True
    settings.EMAIL_BACKEND = 'django.core.mail.backends.dummy.EmailBackend'
    doc = issued()
    with pytest.raises(RuleError, match='Configure a delivery backend'):
        delivery.send_document(blake, doc, uuid.uuid4(), 'sample@example.com', 'Sample', 'Sample')
    assert not DocumentEmail.objects.exists()


def test_owner_refund_resolution_requires_reviewed_tax_credit(owner_client,issued,blake):
    from billing.models import CustomerCredit,Refund
    doc=issued()
    svc.record_stripe_payment('evt_refund_fixture_payment','checkout.session.completed',{'sample':True},doc.pk,10700,0,'pi_refund_fixture',dt.date(2026,12,12))
    refund=svc.record_stripe_refund('evt_refund_fixture','refund.created',{'sample':True},'pi_refund_fixture','re_fixture',4000)
    page=owner_client.get(reverse('billing:document',args=[doc.pk]))
    assert b'Refund needs your decision' in page.content
    response=owner_client.post(reverse('billing:resolve_refund',args=[refund.pk]),{'resolution':'credit_note'})
    assert response.status_code==200
    refund.refresh_from_db();assert refund.resolution=='pending'
    # Today's exemption can change; this credit still reverses the original taxable invoice.
    svc.update_customer(blake,doc.customer,tax_exempt=True,exemption_note='Sample later exemption')
    credit=svc.create_credit_note_draft(blake,doc,[dict(description='Sample returned work',unit_cents=3738,taxable=True)])
    assert svc.preview_totals(credit)[0].total_cents==4000
    assert not CustomerCredit.objects.exists()
    response=owner_client.post(reverse('billing:resolve_refund',args=[refund.pk]),{'resolution':'credit_note','credit_note':credit.pk})
    assert response.status_code==302
    refund.refresh_from_db();credit.refresh_from_db();doc.refresh_from_db()
    assert refund.resolution=='credit_note' and refund.credit_note_id==credit.pk
    assert credit.number and credit.pdf_file and credit.total_cents==4000 and credit.tax_cents==262
    assert credit.customer_snapshot['tax_exempt'] is False
    assert svc.balance_cents(doc)==0 and not CustomerCredit.objects.exists()
    assert not getattr(mail,'outbox',[])
