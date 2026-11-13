"""Hosted Checkout transport. The local ledger changes only after a verified webhook.

One active reservation per invoice, and stable Stripe idempotency keys, prevent concurrent
checkouts or ambiguous retries from silently creating a second payment attempt.
"""
import datetime as dt
import secrets
import string
from urllib.parse import urlparse
import stripe
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from . import audit, pdf, services as svc
from .models import Actor, CheckoutAttempt, Document
from .permissions import ActorRef, RuleError

ACTIVE=['creating','open','processing']

def plain(value):
    return value.to_dict() if hasattr(value,'to_dict') else value


def stripe_client():
    key=settings.STRIPE_SECRET_KEY
    live=settings.CCB_STRIPE_LIVEMODE
    if live and not settings.CCB_STRIPE_LIVE_ENABLED:
        raise RuleError('Live Stripe payments have not been enabled.')
    prefixes=('rk_live_','sk_live_') if live else ('rk_test_','sk_test_')
    if not key or not key.startswith(prefixes):
        raise RuleError('Stripe credentials do not match the configured environment.')
    return stripe.StripeClient(key,stripe_version=settings.STRIPE_API_VERSION,max_network_retries=2,http_client=stripe.RequestsClient(timeout=15))


def enabled():
    if not settings.CCB_STRIPE_ENABLED:
        raise RuleError('Online payments are not enabled yet.')


def active(invoice):
    return CheckoutAttempt.objects.filter(invoice=invoice,status__in=ACTIVE).first()


def _checkout_url(url):
    parsed=urlparse(url or '')
    if parsed.scheme!='https' or parsed.hostname!='checkout.stripe.com' or parsed.username or parsed.password or parsed.port not in (None,443):
        raise RuleError('Stripe did not return a valid hosted Checkout URL.')
    return url


def create_session(invoice,amount_cents,request_id):
    enabled()
    client=stripe_client()
    with transaction.atomic():
        inv=Document.objects.select_for_update().get(pk=invoice.pk)
        svc.check_online_amount(inv,amount_cents)
        attempt=CheckoutAttempt.objects.select_for_update().filter(request_id=request_id).first()
        if attempt and (attempt.invoice_id!=inv.pk or attempt.amount_cents!=amount_cents):
            raise RuleError('This payment submission belongs to another amount or invoice.')
        current=active(inv)
        if current:
            if current.amount_cents!=amount_cents:
                raise RuleError('A checkout is already open. Close it before choosing a different amount.')
            attempt=current
        elif attempt:
            raise RuleError('This payment submission is already closed. Reload the invoice.')
        else:
            attempt=CheckoutAttempt.objects.create(invoice=inv,request_id=request_id,amount_cents=amount_cents,
                integration_identifier='cc-billing-'+''.join(secrets.choice(string.ascii_lowercase) for _ in range(8)),
                expires_at=timezone.now()+dt.timedelta(hours=1))
            audit.record(ActorRef(Actor.SYSTEM,'checkout'),'checkout_reserved','document',inv.pk,after={'checkout_id':attempt.pk,'amount_cents':amount_cents})
        if attempt.status=='processing':
            raise RuleError('A bank payment is still processing. Wait for confirmation before paying again.')
        if attempt.status=='open' and attempt.session_url:
            return attempt
        if timezone.now()-attempt.created_at>dt.timedelta(minutes=15):
            raise RuleError('The previous checkout creation is unconfirmed. Contact us before trying again.')
        number=inv.display_number
        pay_url=f'{pdf.base_url()}/pay/{inv.pay_code}'
    params=dict(mode='payment',client_reference_id=str(inv.pk),currency='usd',
                line_items=[dict(quantity=1,price_data=dict(currency='usd',unit_amount=attempt.amount_cents,product_data=dict(name=f'Payment toward invoice {number}')))],
                metadata={'cc_billing_attempt':str(attempt.request_id),'cc_billing_invoice':str(inv.pk)},
                payment_intent_data={'metadata':{'cc_billing_attempt':str(attempt.request_id),'cc_billing_invoice':str(inv.pk)}},
                integration_identifier=attempt.integration_identifier,expires_at=int(attempt.expires_at.timestamp()),
                success_url=pay_url+'?returned=1',cancel_url=pay_url+'?cancelled=1')
    try:
        session=plain(client.v1.checkout.sessions.create(params,options={'idempotency_key':'cc-billing-'+str(attempt.request_id)}))
        session_id=session['id']
        url=_checkout_url(session['url'])
        if not isinstance(session_id,str) or not session_id.startswith('cs_'):
            raise RuleError('Stripe did not return a valid session.')
    except Exception:
        CheckoutAttempt.objects.filter(pk=attempt.pk,status='creating').update(last_error='Checkout creation is unconfirmed. Retry this submission or contact us.')
        raise RuleError('Checkout could not be confirmed. No second checkout was created; retry or contact us.') from None
    with transaction.atomic():
        Document.objects.select_for_update().get(pk=inv.pk)
        current=CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
        if current.session_id and current.session_id!=session_id:
            raise RuleError('Checkout session conflict. Contact us.')
        current.session_id,current.session_url,current.last_error=session_id,url,''
        if current.status=='creating': current.status='open'
        current.save(update_fields=['session_id','session_url','last_error','status','updated_at'])
        return current


def close_session(invoice):
    enabled()
    client=stripe_client()
    with transaction.atomic():
        inv=Document.objects.select_for_update().get(pk=invoice.pk)
        attempt=active(inv)
        if not attempt: return
        if attempt.status!='open' or not attempt.session_id:
            raise RuleError('This payment is processing or unconfirmed. Contact us before changing it.')
        session_id=attempt.session_id
    try:
        session=plain(client.v1.checkout.sessions.expire(session_id))
    except Exception:
        try:
            session=plain(client.v1.checkout.sessions.retrieve(session_id))
        except Exception:
            raise RuleError('Stripe did not confirm that the checkout closed. Its reservation stays active.') from None
    if session.get('status')!='expired':
        raise RuleError('The checkout has not expired; wait for payment confirmation.')
    with transaction.atomic():
        Document.objects.select_for_update().get(pk=inv.pk)
        attempt=CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
        if attempt.status in ['creating','open']:
            attempt.status='expired';attempt.save(update_fields=['status','updated_at'])
            audit.record(ActorRef.stripe(),'checkout_expired','document',inv.pk,after={'checkout_id':attempt.pk})
