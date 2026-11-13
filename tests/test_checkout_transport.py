"""Real signature verification and PostgreSQL state transitions; Stripe HTTP is replaced locally."""
import copy
import datetime as dt
import hashlib
import hmac
import json
import time
import uuid
from types import SimpleNamespace

import pytest
import stripe
from django.core.cache import cache
from django.db import DatabaseError,transaction
from django.test import Client
from django.urls import reverse
from billing import checkout,pdf,services as svc
from billing.models import AuditEvent,CheckoutAttempt,Document,Payment,Receipt,Refund,StripeEvent
from billing.permissions import RuleError
from .conftest import central,line

pytestmark=pytest.mark.django_db


def typed(data):
    return stripe.StripeObject.construct_from(copy.deepcopy(data),'not-a-real-key')


class FakeStripe:
    def __init__(self):
        self.created={};self.calls=[];self.payment_data={};self.refund_data=[];self.fail_create=False;self.fail_expire=False
        self.v1=SimpleNamespace(checkout=SimpleNamespace(sessions=SimpleNamespace(create=self.create,expire=self.expire)),payment_intents=SimpleNamespace(retrieve=self.retrieve),refunds=SimpleNamespace(list=self.list_refunds))
    def create(self,params,options=None):
        self.calls.append((copy.deepcopy(params),copy.deepcopy(options)))
        if self.fail_create: raise TimeoutError('simulated uncertain response')
        key=options['idempotency_key']
        if key not in self.created:
            self.created[key]=dict(id='cs_test_'+str(len(self.created)+1),url='https://checkout.stripe.com/c/pay/sample-'+str(len(self.created)+1),status='open',**params)
        return typed(self.created[key])
    def expire(self,session):
        if self.fail_expire: raise TimeoutError('simulated uncertain expiration')
        return typed(dict(id=session,status='expired'))
    def retrieve(self,pi_id,params=None):
        if pi_id not in self.payment_data: raise ConnectionError('simulated missing payment')
        return typed(self.payment_data[pi_id])
    def list_refunds(self,params):
        return SimpleNamespace(auto_paging_iter=lambda:iter(typed(r) for r in self.refund_data))


@pytest.fixture(autouse=True)
def clear_cache():
    cache.clear()


@pytest.fixture
def fake(settings,monkeypatch):
    settings.CCB_STRIPE_ENABLED=True
    settings.CCB_STRIPE_ACCEPT_WEBHOOKS=True
    settings.CCB_STRIPE_LIVEMODE=False
    # This is a local signature-fixture string, not a provider credential.
    settings.STRIPE_WEBHOOK_SECRET='sample-local-webhook-signature-fixture'
    fake=FakeStripe()
    monkeypatch.setattr(checkout,'stripe_client',lambda:fake)
    return fake


@pytest.fixture
def invoice(blake,customer,products):
    doc=svc.create_draft(blake,'invoice',customer,[line(products['build'],10000)])
    doc=svc.approve(blake,doc,now=central(2026,12,10))
    svc.mark_sent(blake,doc,now=central(2026,12,10))
    doc.refresh_from_db()
    return doc


def signed(client,event,settings,invalid=False,stamp=None):
    body=json.dumps(event,separators=(',',':')).encode()
    stamp=stamp or int(time.time())
    sig=hmac.new(settings.STRIPE_WEBHOOK_SECRET.encode(),str(stamp).encode()+b'.'+body,hashlib.sha256).hexdigest()
    if invalid: sig='0'*64
    return client.post('/stripe/webhook',body,content_type='application/json',HTTP_STRIPE_SIGNATURE=f't={stamp},v1={sig}')


def event_for(attempt,kind='checkout.session.completed',event_id='evt_sample',paid=True):
    return dict(id=event_id,object='event',type=kind,created=int(dt.datetime(2026,12,12,15,tzinfo=dt.UTC).timestamp()),livemode=False,data={'object':dict(id=attempt.session_id,object='checkout.session',mode='payment',currency='usd',amount_total=attempt.amount_cents,client_reference_id=str(attempt.invoice_id),metadata={'cc_billing_attempt':str(attempt.request_id),'cc_billing_invoice':str(attempt.invoice_id)},payment_status='paid' if paid else 'unpaid',payment_intent='pi_sample')})


def bind_payment(fake,attempt,fee=146):
    fake.payment_data['pi_sample']=dict(id='pi_sample',status='succeeded',currency='usd',amount_received=attempt.amount_cents,metadata={'cc_billing_attempt':str(attempt.request_id),'cc_billing_invoice':str(attempt.invoice_id)},latest_charge={'id':'ch_sample','balance_transaction':{'id':'txn_sample','currency':'usd','fee':fee}})


@pytest.mark.parametrize('text',['0','-5','0.99','107.01','NaN','1.001'])
def test_public_amount_rejections(fake,invoice,client,text):
    response=client.post(reverse('pay_invoice',args=[invoice.pay_code]),{'choice':'other','amount':text,'request_id':str(uuid.uuid4())})
    assert response.status_code==200
    assert not CheckoutAttempt.objects.exists() and not fake.calls


@pytest.mark.parametrize('cents',[100,4000,10700])
def test_checkout_amount_server_owned_and_create_idempotent(fake,invoice,cents):
    key=uuid.uuid4()
    first=checkout.create_session(invoice,cents,key)
    second=checkout.create_session(invoice,cents,key)
    reloaded=checkout.create_session(invoice,cents,uuid.uuid4())
    assert first.pk==second.pk==reloaded.pk and len(fake.created)==1
    assert len(fake.calls)==1
    params,options=fake.calls[0]
    assert params['line_items'][0]['price_data']['unit_amount']==cents and params['line_items'][0]['price_data']['currency']=='usd'
    assert 'payment_method_types' not in params and 'automatic_tax' not in params
    assert params['integration_identifier']==first.integration_identifier and len(first.integration_identifier.rsplit('-',1)[1])==8
    assert options['idempotency_key'].endswith(str(key))
    assert Payment.objects.count()==0


def test_full_choice_ignores_tampered_browser_amount(fake,invoice,client):
    response=client.post(reverse('pay_invoice',args=[invoice.pay_code]),{'choice':'full','amount':'1.00','request_id':str(uuid.uuid4())})
    assert response.status_code==303 and response['Location'].startswith('https://checkout.stripe.com/')
    assert CheckoutAttempt.objects.get().amount_cents==10700


def test_small_balance_and_disabled_links(fake,invoice,blake,client):
    svc.record_manual_payment(blake,invoice,'cash',10625,dt.date(2026,12,12),email_receipt=False)
    with pytest.raises(RuleError): checkout.create_session(invoice,74,uuid.uuid4())
    attempt=checkout.create_session(invoice,75,uuid.uuid4())
    checkout.close_session(invoice)
    svc.record_manual_payment(blake,invoice,'cash',35,dt.date(2026,12,12),email_receipt=False)
    response=client.get(reverse('pay_invoice',args=[invoice.pay_code]))
    assert b'Contact us to settle' in response.content and b'Continue to secure payment' not in response.content
    with pytest.raises(RuleError): checkout.create_session(invoice,40,uuid.uuid4())
    Document.objects.filter(pk=invoice.pk).update(pay_link_disabled=True)
    invoice.refresh_from_db()
    assert not svc.can_pay_online(invoice)
    assert client.get(reverse('pay_invoice',args=[invoice.pay_code])).status_code==404


def test_one_reservation_blocks_other_amount_manual_void_and_credit(fake,invoice,blake):
    attempt=checkout.create_session(invoice,4000,uuid.uuid4())
    with pytest.raises(RuleError): checkout.create_session(invoice,6700,uuid.uuid4())
    with pytest.raises(RuleError): svc.record_manual_payment(blake,invoice,'cash',6700,dt.date(2026,12,12))
    with pytest.raises(RuleError): svc.void(blake,invoice,'sample')
    cn=svc.create_credit_note_draft(blake,invoice,[dict(description='Sample credit',unit_cents=1000,taxable=False)])
    with pytest.raises(RuleError): svc.approve(blake,cn,now=central(2026,12,12))
    assert Payment.objects.count()==0 and NumberCounter_count()==1
    checkout.close_session(invoice)
    attempt.refresh_from_db();assert attempt.status=='expired'
    svc.record_manual_payment(blake,invoice,'cash',6700,dt.date(2026,12,12))


def NumberCounter_count():
    from billing.models import NumberCounter
    return NumberCounter.objects.count()


def test_network_uncertainty_retains_one_idempotent_attempt(fake,invoice):
    fake.fail_create=True
    with pytest.raises(RuleError): checkout.create_session(invoice,4000,uuid.uuid4())
    original=CheckoutAttempt.objects.get();assert original.status=='creating'
    fake.fail_create=False
    recovered=checkout.create_session(invoice,4000,uuid.uuid4())
    assert recovered.pk==original.pk and len(fake.created)==1
    assert fake.calls[0][1]['idempotency_key']==fake.calls[1][1]['idempotency_key']


def test_close_failure_does_not_release_reservation(fake,invoice):
    checkout.create_session(invoice,4000,uuid.uuid4());fake.fail_expire=True
    with pytest.raises(RuleError): checkout.close_session(invoice)
    assert CheckoutAttempt.objects.get().status=='open'


def test_verified_payment_once_with_real_sdk_object(fake,invoice,client,settings):
    attempt=checkout.create_session(invoice,4000,uuid.uuid4());bind_payment(fake,attempt)
    event=event_for(attempt)
    assert signed(client,event,settings).status_code==200
    assert signed(client,event,settings).status_code==200
    event['id']='evt_second_notification'
    assert signed(client,event,settings).status_code==200
    assert Payment.objects.count()==1 and Receipt.objects.count()==1
    payment=Payment.objects.get()
    assert payment.amount_cents==4000 and payment.stripe_fee_cents==146 and payment.received_on==dt.date(2026,12,12)
    assert payment.receipt.kind=='partial' and payment.receipt.email_status=='pending'
    invoice.refresh_from_db();attempt.refresh_from_db()
    assert svc.balance_cents(invoice)==6700 and invoice.status=='partly_paid' and attempt.status=='paid'
    assert AuditEvent.objects.filter(action='payment').count()==1


def test_async_bank_payment_only_records_after_success(fake,invoice,client,settings):
    attempt=checkout.create_session(invoice,10700,uuid.uuid4())
    started=event_for(attempt,paid=False)
    assert signed(client,started,settings).status_code==200
    attempt.refresh_from_db();assert attempt.status=='processing'
    assert Payment.objects.count()==0
    with pytest.raises(RuleError): checkout.create_session(invoice,100,uuid.uuid4())
    with pytest.raises(RuleError): checkout.close_session(invoice)
    bind_payment(fake,attempt)
    done=event_for(attempt,'checkout.session.async_payment_succeeded','evt_bank_success')
    assert signed(client,done,settings).status_code==200
    invoice.refresh_from_db();assert invoice.status=='paid' and Receipt.objects.get().kind=='paid_in_full'
    # Late unpaid completion cannot revert a paid checkout or add a second payment.
    started['id']='evt_late_completed'
    assert signed(client,started,settings).status_code==200
    attempt.refresh_from_db();assert attempt.status=='paid' and Payment.objects.count()==1


def test_async_failure_and_late_completion_do_not_resurrect(fake,invoice,client,settings):
    attempt=checkout.create_session(invoice,10700,uuid.uuid4())
    assert signed(client,event_for(attempt,paid=False),settings).status_code==200
    failed=event_for(attempt,'checkout.session.async_payment_failed','evt_bank_failed',paid=False)
    assert signed(client,failed,settings).status_code==200
    assert signed(client,event_for(attempt,event_id='evt_late',paid=False),settings).status_code==200
    attempt.refresh_from_db();assert attempt.status=='failed' and not Payment.objects.exists()
    checkout.create_session(invoice,4000,uuid.uuid4())


@pytest.mark.parametrize('mutate',[lambda e:e.update(livemode=True),lambda e:e['data']['object'].update(currency='eur'),lambda e:e['data']['object'].update(amount_total=4001),lambda e:e['data']['object']['metadata'].update(cc_billing_invoice='999')])
def test_mismatched_signed_events_never_change_ledger(fake,invoice,client,settings,mutate):
    attempt=checkout.create_session(invoice,4000,uuid.uuid4());bind_payment(fake,attempt)
    event=event_for(attempt);mutate(event)
    assert signed(client,event,settings).status_code==400
    assert not Payment.objects.exists() and not Receipt.objects.exists()


def test_bad_signature_and_success_page_never_record_money(fake,invoice,client,settings):
    attempt=checkout.create_session(invoice,4000,uuid.uuid4())
    assert signed(client,event_for(attempt),settings,invalid=True).status_code==400
    assert signed(client,event_for(attempt),settings,stamp=int(time.time())-1000).status_code==400
    assert client.get(reverse('pay_invoice',args=[invoice.pay_code])+'?returned=1&session_id=cs_attacker').status_code==200
    assert not Payment.objects.exists()
    assert client.get('/stripe/webhook').status_code==405


def test_transient_fee_lookup_retries_without_partial_state(fake,invoice,client,settings):
    attempt=checkout.create_session(invoice,4000,uuid.uuid4())
    event=event_for(attempt)
    assert signed(client,event,settings).status_code==503
    assert not Payment.objects.exists() and not StripeEvent.objects.exists()
    bind_payment(fake,attempt)
    fake.payment_data['pi_sample']['latest_charge']['balance_transaction']=None
    assert signed(client,event,settings).status_code==503
    bind_payment(fake,attempt)
    assert signed(client,event,settings).status_code==200 and Payment.objects.count()==1


def refund_event(attempt,event_id='evt_refund',refund_id='re_sample',amount=1000,status='succeeded'):
    return dict(id=event_id,object='event',livemode=False,type='refund.created',created=int(time.time()),data={'object':dict(id=refund_id,object='refund',status=status,currency='usd',amount=amount,payment_intent='pi_sample')})


def test_refund_recorded_once_never_resolves_or_emails(fake,invoice,client,settings):
    attempt=checkout.create_session(invoice,10700,uuid.uuid4());bind_payment(fake,attempt)
    assert signed(client,event_for(attempt),settings).status_code==200
    event=refund_event(attempt,amount=4000)
    assert signed(client,event,settings).status_code==200
    event['id']='evt_refund_updated';event['type']='refund.updated'
    assert signed(client,event,settings).status_code==200
    refund=Refund.objects.get();assert refund.resolution=='pending' and refund.amount_cents==4000
    assert Payment.objects.count()==1 and Receipt.objects.count()==1
    invoice.refresh_from_db();assert svc.balance_cents(invoice)==0 and invoice.status=='paid'


def test_charge_refund_pagination_includes_all_refunds(fake,invoice,client,settings):
    attempt=checkout.create_session(invoice,10700,uuid.uuid4());bind_payment(fake,attempt)
    assert signed(client,event_for(attempt),settings).status_code==200
    fake.refund_data=[dict(id='re_'+str(i),status='succeeded',currency='usd',amount=100,payment_intent='pi_sample') for i in range(15)]
    event=dict(id='evt_multiple_refunds',object='event',livemode=False,type='charge.refunded',created=int(time.time()),data={'object':dict(id='ch_sample',object='charge',payment_intent='pi_sample',refunds={'data':fake.refund_data[:10],'has_more':True})})
    assert signed(client,event,settings).status_code==200
    assert signed(client,event,settings).status_code==200
    assert Refund.objects.count()==15 and sum(Refund.objects.values_list('amount_cents',flat=True))==1500


def test_refund_before_payment_is_retryable(fake,invoice,client,settings):
    attempt=checkout.create_session(invoice,10700,uuid.uuid4());bind_payment(fake,attempt)
    event=refund_event(attempt)
    assert signed(client,event,settings).status_code==503
    assert not Refund.objects.exists() and not StripeEvent.objects.exists()
    assert signed(client,event_for(attempt),settings).status_code==200
    assert signed(client,event,settings).status_code==200 and Refund.objects.count()==1


def test_public_pdf_requires_capability_and_verified_file(fake,invoice,blake,client,settings,tmp_path):
    settings.MEDIA_ROOT=tmp_path
    doc=svc.create_draft(blake,'invoice',invoice.customer,[dict(description='Sample',unit_cents=1000,taxable=False)])
    doc=svc.approve(blake,doc,now=central(2026,12,10),render_pdf=pdf.render_approved)
    svc.mark_sent(blake,doc)
    response=client.get(reverse('public_pdf',args=[doc.pay_code]))
    assert response.status_code==200 and response.content.startswith(b'%PDF') and response['Referrer-Policy']=='no-referrer'
    assert client.get('/pay/not-a-real-invoice').status_code==404
    page=client.get(reverse('pay_invoice',args=[doc.pay_code]))
    assert page.status_code==200 and b'Continue to secure payment' in page.content and b'owner@example.com' not in page.content
    strict=Client(enforce_csrf_checks=True)
    assert strict.post(reverse('pay_invoice',args=[doc.pay_code]),{'choice':'full','request_id':str(uuid.uuid4())}).status_code==403


def test_checkout_facts_and_terminal_state_guard(fake,invoice):
    attempt=checkout.create_session(invoice,4000,uuid.uuid4())
    for fields in [dict(amount_cents=5000),dict(session_id='cs_different'),dict(request_id=uuid.uuid4())]:
        with pytest.raises(DatabaseError),transaction.atomic():
            CheckoutAttempt.objects.filter(pk=attempt.pk).update(**fields)
    checkout.close_session(invoice)
    with pytest.raises(DatabaseError),transaction.atomic():
        CheckoutAttempt.objects.filter(pk=attempt.pk).update(status='open')
    with pytest.raises(DatabaseError),transaction.atomic():
        attempt.delete()


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_checkouts_share_one_reservation(fake,invoice):
    from concurrent.futures import ThreadPoolExecutor
    from django.db import close_old_connections
    import threading
    barrier=threading.Barrier(2)
    def run():
        close_old_connections()
        try:
            barrier.wait()
            return checkout.create_session(Document.objects.get(pk=invoice.pk),4000,uuid.uuid4()).pk
        finally: close_old_connections()
    with ThreadPoolExecutor(max_workers=2) as pool:
        ids=list(pool.map(lambda _:run(),range(2)))
    assert len(set(ids))==1 and CheckoutAttempt.objects.count()==1
    assert len({options['idempotency_key'] for _,options in fake.calls})==1
    assert len(fake.created)==1


def test_stripe_environment_gates_reject_wrong_keys(settings):
    settings.STRIPE_SECRET_KEY='rk_'+'live_'+'sample-not-a-key'
    settings.CCB_STRIPE_LIVEMODE=False
    with pytest.raises(RuleError): checkout.stripe_client()
    settings.CCB_STRIPE_LIVEMODE=True
    settings.CCB_STRIPE_LIVE_ENABLED=False
    with pytest.raises(RuleError): checkout.stripe_client()


def test_already_expired_checkout_can_be_reconciled(fake,invoice):
    attempt=checkout.create_session(invoice,4000,uuid.uuid4())
    fake.fail_expire=True
    fake.v1.checkout.sessions.retrieve=lambda session_id:typed(dict(id=session_id,status='expired'))
    checkout.close_session(invoice)
    attempt.refresh_from_db();assert attempt.status=='expired'
