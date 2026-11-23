"""Capability-link invoice view, hosted Checkout redirect, and signed webhook endpoint."""
import re
import uuid
import stripe
from django.conf import settings
from django.core.cache import cache
from django.http import Http404, HttpResponse
from django.shortcuts import get_object_or_404,render
from django.urls import reverse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods,require_POST,require_GET
from . import checkout,delivery,money,services as svc,stripe_events
from .forms import PayForm
from .models import Document
from .permissions import BillingError


def private_response(response):
    response['Cache-Control']='no-store, private'
    # same-origin, not no-referrer: with no-referrer, browsers send "Origin: null" on the page's own
    # Pay form and Django's CSRF check refuses it. same-origin still sends nothing to Stripe or other sites.
    response['Referrer-Policy']='same-origin'
    response['X-Robots-Tag']='noindex, nofollow'
    response['Content-Security-Policy']="default-src 'none'; style-src 'unsafe-inline'; img-src 'self' data:; font-src 'self'; form-action 'self' https://checkout.stripe.com; frame-ancestors 'none'; base-uri 'none'"
    return response


def invoice_for(code):
    if not re.fullmatch(r'[A-Za-z0-9_-]{12,24}',code): raise Http404
    return get_object_or_404(Document.objects.select_related('customer'),pay_code=code,kind='invoice',pay_link_disabled=False)


def limited(code):
    key='checkout-post:'+code
    if cache.add(key,1,60): return False
    try: return cache.incr(key)>20
    except ValueError: return False


@require_http_methods(['GET','POST'])
def pay(request,code):
    inv=invoice_for(code)
    balance=max(svc.balance_cents(inv),0)
    possible=svc.can_pay_online(inv) and not inv.pay_link_disabled
    pending=checkout.active(inv)
    form=PayForm(request.POST or None,initial={'request_id':uuid.uuid4()})
    message=''
    if inv.status=='paid': message='Paid in full. Thank you.'
    elif inv.status=='void': message='This invoice has been voided. Contact us with any questions.'
    elif balance<50: message='Contact us to settle this balance.'
    elif not possible: message='Online payment is not available for this invoice. Contact us.'
    elif not settings.CCB_STRIPE_ENABLED: message='Online payment is not enabled yet. Contact us to arrange payment.'
    elif request.GET.get('returned'): message='Your payment is being confirmed. The balance changes only after Stripe confirms payment.'
    elif request.GET.get('cancelled'): message='You returned from Checkout. Close the open checkout below before choosing a different amount.'
    if request.method=='POST':
        if limited(code): return private_response(HttpResponse('Too many payment attempts. Please wait a minute.',status=429))
        if not possible or not settings.CCB_STRIPE_ENABLED:
            form.add_error(None,message or 'This invoice cannot be paid online.')
        elif form.is_valid():
            amount=balance if form.cleaned_data['choice']=='full' else form.cleaned_data['amount']
            try:
                attempt=checkout.create_session(inv,amount,form.cleaned_data['request_id'])
                if attempt.status=='open':
                    return private_response(HttpResponse(status=303,headers={'Location':attempt.session_url}))
                form.add_error(None,'Payment is being confirmed. Do not start another checkout.')
            except BillingError as exc:
                form.add_error(None,str(exc))
            pending=checkout.active(inv)
    pending_message=''
    if pending:
        pending_message={'creating':'Checkout creation is unconfirmed. Retry the same form or contact us.','open':'A secure checkout is open for '+money.format_usd(pending.amount_cents)+'.','processing':'Your bank payment is processing. Wait for confirmation before paying again.'}.get(pending.status,'')
    # Reuse the existing reservation nonce in retries; a page reload cannot make a second session.
    if request.method=='GET' and pending:
        form.initial.update(request_id=pending.request_id,choice='other' if pending.amount_cents!=balance else 'full',amount=money.format_usd(pending.amount_cents))
    total_paid=max(inv.total_cents-balance,0)
    downloadable=bool(inv.pdf_file) and inv.status not in ['draft','void']
    response=render(request,'billing/pay.html',dict(business=inv.business_snapshot or {},invoice_number=inv.display_number,customer_name=(inv.customer_snapshot or {}).get('name',''),
        total=money.format_usd(inv.total_cents),paid=money.format_usd(total_paid),balance=money.format_usd(balance),status_message=message,
        can_checkout=possible and (not pending or pending.status!='processing'),checkout_enabled=settings.CCB_STRIPE_ENABLED,
        pending_message=pending_message,amount_hint=('Only the full balance can be paid online.' if balance<svc.BusinessSettings.load().min_online_payment_cents else 'Minimum payment: '+money.format_usd(svc.BusinessSettings.load().min_online_payment_cents)+'. Maximum: '+money.format_usd(balance)+'.'),form=form,download_url=reverse('public_pdf',args=[code]) if downloadable else '',
        cancel_available=bool(pending and pending.status=='open' and settings.CCB_STRIPE_ENABLED),cancel_url=reverse('close_checkout',args=[code])))
    return private_response(response)


@require_GET
def invoice_pdf(request,code):
    inv=invoice_for(code)
    if inv.status in ['draft','void']: raise Http404
    try: data=delivery.stored_pdf(inv.pdf_file,inv.pdf_sha256)
    except BillingError: raise Http404 from None
    return private_response(HttpResponse(data,content_type='application/pdf',headers={'Content-Disposition':f'attachment; filename="{inv.display_number}.pdf"'}))


@require_POST
def close_checkout(request,code):
    inv=invoice_for(code)
    if limited(code): return private_response(HttpResponse(status=429))
    try: checkout.close_session(inv)
    except BillingError as exc: return private_response(HttpResponse(str(exc),status=409,content_type='text/plain'))
    return private_response(HttpResponse(status=303,headers={'Location':reverse('pay_invoice',args=[code])}))


@csrf_exempt
@require_POST
def webhook(request):
    try:
        if len(request.body)>512*1024: return HttpResponse(status=413)
        event=stripe_events.verify(request.body,request.headers.get('Stripe-Signature',''))
        stripe_events.process(event)
    except stripe_events.RetryEvent:
        return HttpResponse('Webhook verification or reconciliation is temporarily unavailable.',status=503)
    except (stripe.SignatureVerificationError,ValueError,TypeError,KeyError,OverflowError,BillingError):
        return HttpResponse('Invalid or mismatched webhook.',status=400)
    return HttpResponse('ok',content_type='text/plain')
