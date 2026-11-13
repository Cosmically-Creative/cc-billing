"""Verify signed Stripe events; correlate with local Checkout reservations; record once.

No money moves from a browser return URL. Refunds remain undecided until the owner acts.
Only the event identifiers and fields needed for reconciliation are retained here.
"""
import datetime as dt
import hashlib
import uuid
import stripe
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from . import audit, checkout, services as svc
from .models import CheckoutAttempt, Document, Payment, StripeEvent
from .permissions import ActorRef, RuleError


class RetryEvent(Exception):
    """Transient Stripe lookup or out-of-order event; return 503 so Stripe retries."""


def verify(payload,signature):
    if not settings.CCB_STRIPE_ACCEPT_WEBHOOKS or not settings.STRIPE_WEBHOOK_SECRET:
        raise RetryEvent('Webhook processing is not configured.')
    event=checkout.plain(stripe.Webhook.construct_event(payload,signature,settings.STRIPE_WEBHOOK_SECRET,tolerance=300))
    if event.get('livemode') is not settings.CCB_STRIPE_LIVEMODE:
        raise RuleError('Event environment mismatch.')
    if settings.CCB_STRIPE_LIVEMODE and not settings.CCB_STRIPE_LIVE_ENABLED:
        raise RuleError('Live processing is not enabled.')
    if not isinstance(event.get('id'),str) or len(event['id'])>64 or not isinstance(event.get('type'),str) or len(event['type'])>80:
        raise RuleError('Invalid event identifiers.')
    return event


def minimal(event,obj):
    return dict(id=event['id'],type=event['type'],created=event.get('created'),livemode=event['livemode'],object_id=obj.get('id'))


def _finish(event,result,obj):
    with transaction.atomic():
        if svc._claim_event(event['id'],event['type'],minimal(event,obj)):
            svc._finish_event(event['id'],result)


def _attempt(obj):
    metadata=obj.get('metadata') or {}
    raw=metadata.get('cc_billing_attempt')
    if not raw: return None
    try: key=uuid.UUID(raw)
    except (ValueError,TypeError,AttributeError): raise RuleError('Invalid Checkout metadata.') from None
    attempt=CheckoutAttempt.objects.filter(request_id=key).first()
    if not attempt: raise RuleError('Unknown managed Checkout attempt.')
    if (obj.get('currency')!='usd' or type(obj.get('amount_total')) is not int or obj.get('amount_total')!=attempt.amount_cents or
        metadata.get('cc_billing_invoice')!=str(attempt.invoice_id) or obj.get('client_reference_id')!=str(attempt.invoice_id) or
        (attempt.session_id and obj.get('id')!=attempt.session_id)):
        raise RuleError('Checkout does not match its local reservation.')
    return attempt


def _payment_details(obj,attempt):
    pi_id=obj.get('payment_intent')
    if not isinstance(pi_id,str) or not pi_id.startswith('pi_'): raise RuleError('A paid session must identify its payment.')
    try:
        pi=checkout.plain(checkout.stripe_client().v1.payment_intents.retrieve(pi_id,{'expand':['latest_charge.balance_transaction']}))
    except Exception: raise RetryEvent('Payment verification lookup failed.') from None
    meta=pi.get('metadata') or {}
    if pi.get('status')!='succeeded' or pi.get('currency')!='usd' or pi.get('amount_received')!=attempt.amount_cents or meta.get('cc_billing_attempt')!=str(attempt.request_id) or meta.get('cc_billing_invoice')!=str(attempt.invoice_id):
        raise RuleError('Payment does not match the Checkout reservation.')
    charge=pi.get('latest_charge')
    balance=charge.get('balance_transaction') if isinstance(charge,dict) else None
    if not isinstance(balance,dict): raise RetryEvent('Stripe fee details are not available yet.')
    fee=balance.get('fee')
    if type(fee) is not int or fee<0 or balance.get('currency')!='usd': raise RuleError('Invalid fee currency or amount.')
    return pi_id,fee


def _session_event(event,obj):
    attempt=_attempt(obj)
    if attempt is None:
        _finish(event,'Ignored: not a managed billing Checkout',obj);return
    kind=event['type']
    if kind=='checkout.session.async_payment_succeeded' and obj.get('payment_status')!='paid':
        raise RuleError('Successful asynchronous event is not paid.')
    paid=obj.get('payment_status')=='paid' and kind in ['checkout.session.completed','checkout.session.async_payment_succeeded']
    details=_payment_details(obj,attempt) if paid else None
    with transaction.atomic():
        inv=Document.objects.select_for_update().get(pk=attempt.invoice_id)
        attempt=CheckoutAttempt.objects.select_for_update().get(pk=attempt.pk)
        if StripeEvent.objects.filter(pk=event['id'],processed_at__isnull=False).exists(): return
        if details:
            pi_id,fee=details
            stamp=event.get('created')
            if type(stamp) is not int: raise RuleError('Payment event date missing.')
            received_on=timezone.localdate(dt.datetime.fromtimestamp(stamp,dt.UTC))
            payment=svc.record_stripe_payment(event['id'],kind,minimal(event,obj),inv.pk,attempt.amount_cents,fee,pi_id,received_on)
            if payment or Payment.objects.filter(stripe_payment_intent_id=pi_id,invoice=inv).exists():
                attempt.status='paid';attempt.payment_intent_id=pi_id
            else:
                # Keep the reservation unresolved for operator review; the event is retained by svc.
                attempt.last_error='Verified payment requires owner reconciliation.'
        else:
            if not svc._claim_event(event['id'],kind,minimal(event,obj)): return
            if attempt.status not in ['paid','failed','expired']:
                if kind=='checkout.session.completed':
                    attempt.status='processing'
                    if isinstance(obj.get('payment_intent'),str): attempt.payment_intent_id=obj['payment_intent']
                elif kind=='checkout.session.async_payment_failed': attempt.status='failed'
                elif kind=='checkout.session.expired' and attempt.status!='processing': attempt.status='expired'
            svc._finish_event(event['id'],'Checkout '+attempt.status)
            audit.record(ActorRef.stripe(),'checkout_'+attempt.status,'document',inv.pk,after={'checkout_id':attempt.pk,'event_id':event['id']})
        attempt.session_id=obj['id']
        attempt.save(update_fields=['status','session_id','payment_intent_id','last_error','updated_at'])


def _refund_managed(pi_id):
    if not isinstance(pi_id,str): return False
    if Payment.objects.filter(stripe_payment_intent_id=pi_id).exists(): return True
    if CheckoutAttempt.objects.filter(payment_intent_id=pi_id).exists(): raise RetryEvent('Refund arrived before the payment was recorded.')
    try: pi=checkout.plain(checkout.stripe_client().v1.payment_intents.retrieve(pi_id))
    except Exception: raise RetryEvent('Refund payment lookup failed.') from None
    metadata=pi.get('metadata') or {}
    if metadata.get('cc_billing_attempt'): raise RetryEvent('Managed payment has not been recorded yet.')
    return False


def _refunds(event,obj):
    if event['type']=='charge.refunded':
        try: refunds=[checkout.plain(r) for r in checkout.stripe_client().v1.refunds.list({'charge':obj['id'],'limit':100}).auto_paging_iter()]
        except Exception: raise RetryEvent('Refund pagination lookup failed.') from None
    else: refunds=[obj]
    with transaction.atomic():
        if not svc._claim_event(event['id'],event['type'],minimal(event,obj)): return
        for refund in refunds:
            if refund.get('status')!='succeeded': continue
            pi_id=refund.get('payment_intent') or obj.get('payment_intent')
            if not _refund_managed(pi_id): continue
            amount=refund.get('amount')
            if refund.get('currency')!='usd' or type(amount) is not int or amount<=0:
                raise RuleError('Invalid refund currency or amount.')
            if not isinstance(refund.get('id'),str): raise RuleError('Refund identifier missing.')
            synthetic='rf_'+hashlib.sha256((event['id']+':'+refund['id']).encode()).hexdigest()[:61]
            svc.record_stripe_refund(synthetic,event['type'],minimal(event,obj),pi_id,refund['id'],amount)
        svc._finish_event(event['id'],'Refunds reconciled; owner decides resolution')


def process(event):
    if StripeEvent.objects.filter(pk=event['id'],processed_at__isnull=False).exists(): return
    obj=(event.get('data') or {}).get('object')
    if not isinstance(obj,dict): raise RuleError('Event object missing.')
    if event['type'] in ['checkout.session.completed','checkout.session.async_payment_succeeded','checkout.session.async_payment_failed','checkout.session.expired']:
        _session_event(event,obj)
    elif event['type'] in ['refund.created','refund.updated','charge.refunded']:
        _refunds(event,obj)
    else:
        _finish(event,'Ignored: unsupported event type',obj)
