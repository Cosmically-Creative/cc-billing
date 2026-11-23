"""Owner UI: read views plus explicit CSRF-protected actions through billing services."""
import calendar
import csv
import datetime as dt
import uuid
from functools import wraps
from types import SimpleNamespace
from urllib.parse import urlencode

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q, Sum
from django.http import Http404, HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from . import audit, delivery, money, pdf, services as svc, theme
from .forms import CustomerForm, DraftForm, LineFormSet, PaymentForm, PeriodForm, SendForm, SettingsForm, TaxLocationForm, RefundResolutionForm
from .models import AuditEvent, BusinessSettings, Customer, Document, LineItem, PaidCopy, Payment, Product, Receipt, Refund, RecurringTemplate, TaxLocationProposal
from .permissions import ActorRef, BillingError

D, S = Document, Document.Status


def owner(view):
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not request.user.is_superuser:
            return HttpResponseForbidden('Owner access required.')
        request.actor = ActorRef.blake(request.user.pk)
        return view(request, *args, **kwargs)
    return wrapped


def ui(request, template, title, **context):
    links = [('home', 'Dashboard', {}), ('billing:documents', 'Quotes', {'kind': 'quote'}), ('billing:documents', 'Invoices', {'kind': 'invoice'}),
             ('billing:customers', 'Customers', {}), ('billing:payments', 'Payments', {}), ('billing:recurring', 'Recurring', {}), ('billing:settings', 'Settings', {})]
    nav = [dict(label=label, url=reverse(name, kwargs=kw), active=request.path == reverse(name, kwargs=kw)) for name, label, kw in links]
    context.update(title=title, nav_items=nav, theme=theme.current(request), theme_choices=theme.CHOICES)
    response = render(request, template, context)
    response['Cache-Control'] = 'no-store, private'
    response['Referrer-Policy'] = 'same-origin'
    return response


def doc_row(d):
    total = svc.preview_totals(d)[0].total_cents if d.is_draft else d.total_cents
    return dict(url=reverse('billing:document', args=[d.pk]), statuses=svc.badges(d), cells=[d.display_number or 'Draft', d.customer.name, pdf.fmt_date(d.issue_date) or '—', pdf.fmt_date(d.due_date) or '—', ' · '.join(svc.badges(d)), money.format_usd(total), money.format_usd(max(svc.balance_cents(d), 0)) if d.kind == D.Kind.INVOICE and not d.is_draft else '—'])


def table(request, title, columns, rows, actions=(), search=False, filters=(), **extra):
    page = Paginator(rows, 40).get_page(request.GET.get('page'))
    params = request.GET.copy()
    for key in ('page', 'q'):
        params.pop(key, None)
    return ui(request, 'billing/list.html', title, columns=columns, rows=list(page), actions=actions, search=search, query=request.GET.get('q', ''), filters=filters, page_obj=page, extra_query=params.urlencode(), **extra)


def action(label, name, *args):
    return dict(label=label, url=reverse(name, args=args))


@owner
@require_GET
def dashboard(request):
    today = svc.today()
    docs = list(D.objects.select_related('customer').prefetch_related('lines').order_by('-pk'))
    open_docs = [d for d in docs if d.kind == 'invoice' and not d.is_draft and d.status != S.VOID and svc.balance_cents(d) > 0]
    overdue = [d for d in open_docs if d.due_date and d.due_date < today]
    paid = Payment.objects.filter(received_on__gte=today.replace(day=1), received_on__lte=today).aggregate(total=Sum('amount_cents'))['total'] or 0
    metrics = [dict(label='Owed', value=money.format_usd(sum(svc.balance_cents(d) for d in open_docs)), detail=f'{len(open_docs)} open invoices'),
               dict(label='Overdue', value=money.format_usd(sum(svc.balance_cents(d) for d in overdue)), detail=f'{len(overdue)} invoices past due'),
               dict(label='Paid this month', value=money.format_usd(paid), detail=today.strftime('%B %Y'))]
    return ui(request, 'billing/dashboard.html', 'Dashboard', today=today, metrics=metrics,
              review_rows=[dict(url=reverse('billing:document', args=[d.pk]), cells=[d.customer.name, 'Prepared by Ledger — needs review' if d.prepared_by == 'ledger' else 'Draft', money.format_usd(svc.preview_totals(d)[0].total_cents)]) for d in docs if d.is_draft][:10], open_rows=[doc_row(d) for d in open_docs][:10],
              upcoming_rows=[dict(url=reverse('billing:document', args=[d.pk]), cells=[pdf.fmt_date(d.due_date), d.display_number, money.format_usd(svc.balance_cents(d))]) for d in sorted(open_docs, key=lambda d: d.due_date or dt.date.max) if d.due_date and d.due_date >= today][:8],
              activity_rows=[dict(cells=[pdf.fmt_date(e.at.date()), e.get_actor_display(), e.action]) for e in AuditEvent.objects.order_by('-pk')[:10]])


@owner
@require_GET
def documents(request, kind):
    if kind not in ('invoice', 'quote', 'credit_note'):
        raise Http404
    docs = D.objects.filter(kind=kind).select_related('customer').prefetch_related('lines').order_by('-pk')
    query = request.GET.get('q', '').strip()
    if query:
        docs = docs.filter(Q(number__icontains=query) | Q(original_number__icontains=query) | Q(customer__name__icontains=query))
    status = request.GET.get('status', '')
    if status and status != 'overdue':
        docs = docs.filter(status=status)
    rows = [doc_row(d) for d in docs if status != 'overdue' or 'Overdue' in svc.badges(d)]
    filters = [dict(label=label, url='?' + urlencode({'q': query, 'status': value}), active=status == value) for value, label in [('', 'All'), ('draft', 'Draft'), ('approved', 'Approved'), ('sent', 'Sent'), ('partly_paid', 'Partly paid'), ('overdue', 'Overdue'), ('paid', 'Paid'), ('void', 'Void')]]
    actions = [action('+ New '+kind, 'billing:new_document', kind)] if kind != 'credit_note' else []
    return table(request, {'invoice': 'Invoices', 'quote': 'Quotes', 'credit_note': 'Credit notes'}[kind], ['Number', 'Customer', 'Issued', 'Due', 'Status', 'Total', 'Balance'], rows, actions, search=True, filters=filters)


@owner
@require_GET
def customers(request):
    customers = Customer.objects.select_related('tax_jurisdiction').order_by('name')
    query = request.GET.get('q', '').strip()
    if query:
        customers = customers.filter(Q(name__icontains=query) | Q(email__icontains=query) | Q(address__icontains=query) | Q(tax_jurisdiction__name__icontains=query))
    mode = request.GET.get('filter', '')
    if mode == 'missing':
        customers = customers.filter(tax_state='')
    elif mode == 'pending':
        customers = customers.filter(tax_location_pending=True)
    elif mode == 'exempt':
        customers = customers.filter(tax_exempt=True)
    rows = []
    for c in customers:
        balance = sum(max(svc.balance_cents(d), 0) for d in c.documents.filter(kind='invoice'))
        rows.append(dict(url=reverse('billing:customer', args=[c.pk]), cells=[c.name, c.email, str(c.tax_jurisdiction or c.tax_state or 'Not set'), 'Tax-exempt' if c.tax_exempt else c.tax_location_status, money.format_usd(balance)]))
    return table(request, 'Customers', ['Customer', 'Email', 'Tax location', 'Status', 'Open balance'], rows, [action('+ New customer', 'billing:new_customer')], search=True)


@owner
@require_http_methods(['GET', 'POST'])
def customer_edit(request, pk=None):
    c = get_object_or_404(Customer, pk=pk) if pk else None
    form = CustomerForm(request.POST or None, instance=c)
    if request.method == 'POST' and form.is_valid():
        try:
            data = {k: form.cleaned_data[k] for k in form.Meta.fields}
            c = svc.update_customer(request.actor, c, **data) if c else svc.create_customer(request.actor, **data)
            return redirect('billing:customer', c.pk)
        except BillingError as exc:
            form.add_error(None, str(exc))
    return ui(request, 'billing/form.html', 'Edit contact' if c else 'New customer', form=form, submit_label='Save customer')


@owner
@require_http_methods(['GET', 'POST'])
def customer_detail(request, pk):
    c = get_object_or_404(Customer.objects.select_related('tax_jurisdiction'), pk=pk)
    form = TaxLocationForm(request.POST or None, initial=dict(state=c.tax_state or 'IA', jurisdiction=c.tax_jurisdiction_id, checked_on=c.tax_location_checked_on or svc.today(), source=c.tax_location_source))
    if request.method == 'POST' and form.is_valid():
        svc.confirm_tax_location(request.actor, c, **form.cleaned_data)
        return redirect('billing:customer', c.pk)
    return ui(request, 'billing/customer.html', c.name, customer=c, form=form,
              proposals=c.tax_proposals.filter(status='pending'), documents=[doc_row(d) for d in c.documents.select_related('customer').prefetch_related('lines').order_by('-pk')],
              balance=money.format_usd(sum(max(svc.balance_cents(d), 0) for d in c.documents.filter(kind='invoice'))))


@owner
@require_POST
def proposal_decide(request, pk, proposal_id):
    c = get_object_or_404(Customer, pk=pk)
    with transaction.atomic():
        prop = get_object_or_404(TaxLocationProposal.objects.select_for_update(), pk=proposal_id, customer=c, status='pending')
        if request.POST.get('decision') == 'confirm':
            svc.confirm_tax_location(request.actor, c, proposal=prop)
        elif request.POST.get('decision') == 'dismiss':
            prop.status, prop.decided_at = 'rejected', timezone.now()
            prop.save(update_fields=['status', 'decided_at'])
            audit.record(request.actor, 'dismiss_tax_proposal', 'customer', c.pk, after={'proposal_id': prop.pk})
        else:
            return HttpResponse('Choose confirm or dismiss.', status=400)
    return redirect('billing:customer', c.pk)


def _draft_preview(doc, line_data):
    lines = [SimpleNamespace(**{**x, 'taxable': x['taxable']}) for x in line_data]
    ctx = svc.tax_context(doc, doc.customer, svc.today(), lines)
    totals = money.compute_totals([money.LineInput(x['unit_cents'], x['qty_milli'], x['discount_cents'], x['taxable']) for x in line_data], ctx.state_bp, ctx.local_bp, customer_exempt=svc.exemption_for(doc, doc.customer), donated=doc.donated)
    for name in ('subtotal_cents', 'discount_cents', 'taxable_cents', 'tax_cents', 'state_tax_cents', 'local_tax_cents', 'total_cents', 'value_cents'):
        setattr(doc, name, getattr(totals, name))
    for li, result in zip(lines, totals.lines):
        li.line_cents, li.tax_cents, li.tax_rate_bp = result.net_cents, result.tax_cents, result.tax_rate_bp
    data = {f.name: getattr(doc, f.name) for f in D._meta.fields if not f.is_relation}
    data.update(pk=None, is_draft=True, credit_for_id=doc.credit_for_id, tax_jurisdiction_id=getattr(ctx.jurisdiction, 'pk', None), tax_jurisdiction=ctx.jurisdiction,
                tax_label=ctx.label, out_of_state=ctx.out_of_state, state_rate_bp=ctx.state_bp, local_rate_bp=ctx.local_bp,
                customer_snapshot=svc.customer_state(doc.customer),
                business_snapshot={k: getattr(BusinessSettings.load(), k) for k in ['name','address_line1','city_state_zip','email','phone']},
                number='DRAFT — UNAPPROVED', issue_date=None, due_date=None, pay_code='', lines=SimpleNamespace(all=lambda: lines))
    return totals, ctx, SimpleNamespace(**data)


@owner
@require_http_methods(['GET', 'POST'])
def document_edit(request, kind=None, pk=None, credit_for=None):
    invoice = get_object_or_404(D, pk=credit_for, kind='invoice', source='app') if credit_for else None
    doc = get_object_or_404(D, pk=pk) if pk else (D(kind='credit_note', credit_for=invoice, customer=invoice.customer) if invoice else D(kind=kind))
    if doc.kind not in ('invoice', 'quote', 'credit_note') or (not pk and kind == 'credit_note' and not invoice):
        raise Http404
    if not doc.is_draft or doc.source != 'app':
        return HttpResponse('This document is locked.', status=409)
    form = DraftForm(request.POST or None, instance=doc, initial={'customer': request.GET.get('customer')} if not pk and not invoice else None)
    if doc.kind == 'credit_note':
        form.fields['customer'].disabled = True
    initial = [dict(product=li.product_id, description=li.description, quantity=pdf.fmt_qty(li.qty_milli).replace(',', ''), rate=money.format_usd(li.unit_cents).replace('$', ''), discount=money.format_usd(li.discount_cents).replace('$', '') if li.discount_cents else '', discount_label=li.discount_label, taxable=li.taxable) for li in (doc.lines.all() if pk else invoice.lines.all())] if pk or invoice else []
    lines = LineFormSet(request.POST or None, initial=initial, prefix='lines')
    totals = ctx = None
    if request.method == 'POST' and form.is_valid() and lines.is_valid():
        kept = [f for f in lines if f.cleaned_data and not f.cleaned_data.get('DELETE')]
        data = [f.line_data() for f in kept]
        fields = {k: form.cleaned_data[k] for k in form.Meta.fields}
        try:
            totals, ctx, preview = _draft_preview(doc, data)
            mode = request.POST.get('action', 'save')
            if mode == 'preview_pdf':
                return HttpResponse(pdf.render_pdf_bytes(pdf.document_context(preview, payments=[])), content_type='application/pdf', headers={'Cache-Control':'no-store'})
            if mode == 'preview':
                line_amounts = {f.prefix: money.format_usd(r.net_cents) for f, r in zip(kept, totals.lines)}
                return ui(request, 'billing/totals.html', 'Preview totals', totals=totals, tax_context=ctx, line_amounts=line_amounts)
            if mode != 'save':
                raise BillingError('Choose a valid action.')
            if pk:
                saved = svc.update_draft(request.actor, doc, lines=data, **fields)
            elif invoice:
                saved = svc.create_credit_note_draft(request.actor, invoice, data, notes=fields['notes'])
            else:
                saved = svc.create_draft(request.actor, doc.kind, fields.pop('customer'), data, **fields)
            return redirect('billing:document', saved.pk)
        except (BillingError, ValueError, LookupError) as exc:
            form.add_error(None, str(exc))
    if request.POST.get('action') == 'preview':
        return ui(request, 'billing/totals.html', 'Preview totals', form=form, lines=lines, totals=totals, tax_context=ctx)
    return ui(request, 'billing/editor.html', 'Edit '+doc.get_kind_display().lower() if pk else 'New '+doc.get_kind_display().lower(), form=form, lines=lines, doc=doc, editor_products=[dict(id=p.pk, description=p.name, rate=money.format_usd(p.unit_price_cents), taxable=p.default_taxable) for p in Product.objects.filter(active=True)])


@owner
@require_GET
def document_detail(request, pk):
    doc = get_object_or_404(D.objects.select_related('customer').prefetch_related('lines'), pk=pk)
    totals, ctx = svc.preview_totals(doc) if doc.is_draft else (None, None)
    return ui(request, 'billing/document.html', doc.display_number or f'Draft {doc.get_kind_display().lower()}', doc=doc, badges=svc.badges(doc),
              pdf_context=pdf.document_context(doc, payments=[]) if not doc.is_draft else None,
              totals=totals, tax_context=ctx, balance=money.format_usd(max(svc.balance_cents(doc), 0)),
              pending_refunds=[dict(refund=r,form=RefundResolutionForm(doc)) for r in Refund.objects.filter(payment__invoice=doc,resolution='pending')],
              history=AuditEvent.objects.filter(entity='document', entity_id=str(doc.pk)).order_by('-pk'), receipts=Receipt.objects.filter(payment__invoice=doc).select_related('payment'), paid_copies=doc.paid_copies.all(), email_attempts=doc.emails.order_by('-pk'),
              can_send=doc.source == 'app' and doc.status in [S.APPROVED, S.SENT, S.PARTLY_PAID, S.PAID, S.ACCEPTED])


@owner
@require_POST
def document_action(request, pk):
    doc = get_object_or_404(D, pk=pk)
    if doc.source != 'app':
        return HttpResponse('Imported history is read-only.', status=409)
    try:
        mode = request.POST.get('action')
        if mode == 'approve':
            svc.approve(request.actor, doc, render_pdf=pdf.render_approved)
        elif mode == 'mark_sent':
            warnings = svc.mark_sent(request.actor, doc, 'manual')
            for warning in warnings:
                messages.warning(request, warning)
        elif mode == 'void':
            svc.void(request.actor, doc, request.POST.get('reason','').strip())
        elif mode == 'replace':
            replacement = svc.create_replacement(request.actor, doc)
            return redirect('billing:document', replacement.pk)
        elif mode == 'delete':
            svc.delete_draft(request.actor, doc)
            return redirect('billing:documents', doc.kind)
        elif mode in ['accepted', 'declined', 'expired']:
            svc.set_quote_outcome(request.actor, doc, mode)
        elif mode == 'convert':
            converted = svc.convert_quote(request.actor, doc)
            return redirect('billing:document', converted.pk)
        else:
            return HttpResponse('Unknown action.', status=400)
    except (BillingError, ValueError) as exc:
        messages.error(request, str(exc))
    return redirect('billing:document', doc.pk)


@owner
@require_GET
def document_pdf(request, pk, copy_id=None):
    doc = get_object_or_404(D, pk=pk)
    copy = get_object_or_404(PaidCopy, pk=copy_id, invoice=doc) if copy_id else doc
    try:
        data = delivery.stored_pdf(copy.pdf_file, copy.pdf_sha256)
    except BillingError as exc:
        return HttpResponse(str(exc), status=409)
    name = (doc.display_number or 'document') + ('-PAID' if copy_id else '') + '.pdf'
    return HttpResponse(data, content_type='application/pdf', headers={'Content-Disposition': f'attachment; filename="{name}"', 'Cache-Control':'no-store, private'})


@owner
@require_http_methods(['GET', 'POST'])
def document_send(request, pk):
    doc = get_object_or_404(D, pk=pk)
    if doc.is_draft or doc.status == S.VOID or doc.source != 'app':
        return HttpResponse('This document cannot be sent.', status=409)
    form = SendForm(request.POST or None, initial=delivery.document_defaults(doc))
    request_id = request.POST.get('request_id') or str(uuid.uuid4())
    if request.method == 'POST' and form.is_valid():
        try:
            key = uuid.UUID(request_id)
            attempt = delivery.send_document(request.actor, doc, key, form.cleaned_data['to'], form.cleaned_data['subject'], form.cleaned_data['body'])
            if attempt.status == 'sent':
                messages.success(request, 'Email sent. The recipients and PDF are recorded in history.')
                return redirect('billing:document', doc.pk)
            form.add_error(None, attempt.last_error or 'Delivery is still unconfirmed. Check the mailbox before trying again.')
        except (BillingError, ValueError) as exc:
            form.add_error(None, str(exc))
    return ui(request, 'billing/send.html', 'Send '+doc.display_number, doc=doc, form=form, request_id=request_id,
              test_recipients=settings.CCB_EMAIL_ALLOWED_RECIPIENTS)


@owner
@require_http_methods(['GET', 'POST'])
def payment_new(request):
    form = PaymentForm(request.POST or None, initial={'invoice':request.GET.get('invoice'), 'received_on':svc.today(), 'email_receipt':True})
    if request.method == 'POST' and form.is_valid():
        data = dict(form.cleaned_data)
        inv = data.pop('invoice')
        cents = data.pop('amount')
        try:
            svc.record_manual_payment(request.actor, inv, amount_cents=cents, **data)
            messages.success(request, 'Payment recorded. Receipt queued if selected.')
            return redirect('billing:document', inv.pk)
        except (BillingError, ValueError) as exc:
            form.add_error(None, str(exc))
    return ui(request, 'billing/form.html', 'Record check or cash', form=form, submit_label='Record payment')


@owner
@require_GET
def payments(request):
    rows = [dict(url=reverse('billing:document', args=[p.invoice_id]), cells=[pdf.fmt_date(p.received_on), p.invoice.display_number, p.invoice.customer.name, p.get_method_display(), money.format_usd(p.amount_cents), p.receipt.get_email_status_display() if hasattr(p, 'receipt') else '—']) for p in Payment.objects.select_related('invoice__customer', 'receipt').order_by('-received_on','-pk')]
    return table(request, 'Payments', ['Date', 'Invoice', 'Customer', 'Method', 'Amount', 'Receipt'], rows, [action('Record check or cash', 'billing:new_payment')])


@owner
@require_POST
def receipt_resend(request, pk):
    r = get_object_or_404(Receipt, pk=pk)
    try:
        delivery._enabled()
        result = svc.resend_receipt(request.actor, r, pdf.render_paid_copy, delivery.send_receipt_email)
        messages.success(request, 'Receipt sent.') if result.email_status == 'sent' else messages.error(request, 'Receipt delivery failed. Check the mailbox before retrying.')
    except BillingError as exc:
        messages.error(request, str(exc))
    return redirect('billing:document', r.payment.invoice_id)


@owner
@require_http_methods(['GET', 'POST'])
def settings_view(request):
    st = BusinessSettings.load()
    form = SettingsForm(request.POST or None, instance=st)
    if request.method == 'POST' and form.is_valid():
        try:
            svc.update_settings(request.actor, **{k: form.cleaned_data[k] for k in form.Meta.fields})
            return redirect('billing:settings')
        except BillingError as exc:
            form.add_error(None, str(exc))
    return ui(request, 'billing/settings.html', 'Settings', form=form)


@owner
@require_POST
def set_theme(request):
    return theme.set_theme(request)


@owner
@require_GET
def tax_report(request):
    today = svc.today()
    form = PeriodForm(request.GET or None, initial={'start':today.replace(day=1),'end':today.replace(day=calendar.monthrange(today.year,today.month)[1])})
    report = None
    if not request.GET:
        start, end = today.replace(day=1), today.replace(day=calendar.monthrange(today.year,today.month)[1])
    elif form.is_valid():
        start, end = form.cleaned_data['start'], form.cleaned_data['end']
    else:
        return ui(request, 'billing/tax_report.html', 'Tax report', form=form)
    report = svc.tax_report(start, end)
    rows = [dict(jurisdiction=r['jurisdiction'], taxable=money.format_usd(r['taxable_cents']), state=money.format_usd(r['state_tax_cents']), local=money.format_usd(r['local_tax_cents'])) for r in report['jurisdictions']]
    if request.GET.get('format') == 'csv':
        response = HttpResponse(content_type='text/csv', headers={'Content-Disposition':'attachment; filename="tax-report.csv"'})
        writer = csv.writer(response)
        writer.writerow(['Location', 'Taxable sales', 'State tax', 'Local tax'])
        for r in rows:
            writer.writerow([r['jurisdiction'], r['taxable'], r['state'], r['local']])
        for r in report['exempt_sales']:
            writer.writerow(['Exempt: '+r['customer'], money.format_usd(r['sales_cents']), '$0.00', '$0.00'])
        writer.writerow(['Non-taxable sales', money.format_usd(report['non_taxable_cents'])])
        writer.writerow(['Out-of-state sales', money.format_usd(report['out_of_state_cents'])])
        writer.writerow(['Tax received', '', money.format_usd(report['received']['state_tax_cents']), money.format_usd(report['received']['local_tax_cents'])])
        return response
    return ui(request, 'billing/tax_report.html', 'Tax report', form=form, rows=rows, report=report,
              invoiced={k:money.format_usd(v) for k,v in report['invoiced'].items()}, received={k:money.format_usd(v) for k,v in report['received'].items()},
              exempt_rows=[dict(customer=r['customer'], amount=money.format_usd(r['sales_cents'])) for r in report['exempt_sales']], non_taxable=money.format_usd(report['non_taxable_cents']), out_of_state=money.format_usd(report['out_of_state_cents']), csv_query=urlencode({'start':start,'end':end,'format':'csv'}))


@owner
@require_GET
def recurring(request):
    rows = [dict(cells=[r.customer.name, r.get_interval_display(), pdf.fmt_date(r.next_date), 'Active' if r.active else 'Paused']) for r in RecurringTemplate.objects.select_related('customer').order_by('next_date')]
    return table(request, 'Recurring · January release', ['Customer', 'Interval', 'Next renewal', 'Status'], rows)


@owner
@require_GET
def void_pdf(request, pk):
    doc = get_object_or_404(D, pk=pk, status='void')
    replacement = doc.replaced_by.exclude(status='draft').order_by('pk').first()
    return HttpResponse(pdf.render_pdf_bytes(pdf.document_context(doc, stamp='void', replacement=replacement)), content_type='application/pdf', headers={'Cache-Control':'no-store, private', 'Content-Disposition':f'attachment; filename="{doc.display_number}-VOID.pdf"'})


@owner
@require_POST
def refund_resolve(request, pk):
    refund=get_object_or_404(Refund.objects.select_related('payment__invoice'),pk=pk,resolution='pending')
    invoice=refund.payment.invoice
    if invoice.source!='app': return HttpResponse('Imported history is read-only.',status=409)
    form=RefundResolutionForm(invoice,request.POST)
    if form.is_valid():
        try:
            svc.resolve_refund(request.actor,refund,form.cleaned_data['resolution'],credit_note=form.cleaned_data.get('credit_note'),render_pdf=pdf.render_approved)
            messages.success(request,'Refund resolution recorded. No email sent.')
            return redirect('billing:document',invoice.pk)
        except BillingError as exc:
            form.add_error(None,str(exc))
    return ui(request,'billing/form.html','Review refund resolution',form=form,submit_label='Confirm refund resolution')
