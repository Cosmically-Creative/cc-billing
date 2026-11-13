"""Read/draft operations shared by the private JSON API and MCP transport.

Only these operations are exposed. All mutations call the existing permission-checked services.
No actor, number, status, frozen tax value or approval flag can be supplied by a caller.
"""
import base64
import datetime as dt
from django.core.validators import validate_email
from django.core.exceptions import ValidationError
from django.db import transaction
from django.shortcuts import get_object_or_404
from django.db.models import Q, Sum
from jsonschema import Draft202012Validator, FormatChecker, validators
from . import audit, delivery, services as svc
from .models import Actor, AuditEvent, Customer, Document, PaidCopy, Payment, Product, Receipt, RecurringTemplate, TaxJurisdiction
from .permissions import BillingError, PermissionDenied, RuleError, require


def string(maximum=500, **kw):
    return dict(type='string', maxLength=maximum, **kw)


def integer(minimum=0, maximum=2_000_000_000):
    return dict(type='integer', minimum=minimum, maximum=maximum)


def obj(properties, required=()):
    return dict(type='object', properties=properties, required=list(required), additionalProperties=False)


ID = integer(1)
PAGING = dict(offset=integer(0, 1_000_000), limit=integer(1, 100))
LINE = obj(dict(product_id=ID, description=string(), unit_cents=integer(), qty_milli=integer(1), discount_cents=integer(), discount_label=string(200), taxable=dict(type='boolean')), ['unit_cents'])
LINES = dict(type='array', items=LINE, minItems=1, maxItems=200)
CONTACT = dict(name=string(200, minLength=1), email=string(254), phone=string(40), address=string(4000), notes=string(10000))
DRAFT_FIELDS = dict(customer_id=ID, notes=string(10000), email_subject=string(200), email_body=string(20000))
StrictValidator = validators.extend(Draft202012Validator, type_checker=Draft202012Validator.TYPE_CHECKER.redefine("integer",lambda checker,value:isinstance(value,int) and not isinstance(value,bool)))
SPECS = {}
HANDLERS = {}


def operation(name, description, schema, capability='view'):
    def register(func):
        SPECS[name] = dict(name=name, description=description, inputSchema=schema,
                           annotations=dict(readOnlyHint=capability=='view', destructiveHint=False, openWorldHint=False))
        HANDLERS[name] = (func, capability)
        return func
    return register


def page(queryset, args, serializer):
    offset, limit = args.get('offset',0), args.get('limit',50)
    total = queryset.count()
    return dict(items=[serializer(x) for x in queryset[offset:offset+limit]], total=total, next_offset=offset+limit if offset+limit<total else None)


def customer_json(c):
    invoices = c.documents.filter(kind='invoice')
    return {**svc.customer_state(c), **audit._jsonable(svc._location_state(c)), 'id':c.pk,
            'tax_jurisdiction_id':c.tax_jurisdiction_id, 'tax_location_status':c.tax_location_status,
            'open_balance_cents':sum(max(svc.balance_cents(d),0) for d in invoices),
            'credit_cents':c.credits.aggregate(total=Sum('amount_cents'))['total'] or 0}


def document_json(d):
    data = audit._jsonable(svc.document_state(d))
    data.update(id=d.pk, customer_name=d.customer.name, original_number=d.original_number, source=d.source,
                prepared_by=d.prepared_by, balance_cents=max(svc.balance_cents(d),0), badges=svc.badges(d),
                tax_label=d.tax_label, tax_state=d.tax_state, tax_jurisdiction_id=d.tax_jurisdiction_id,
                state_tax_cents=d.state_tax_cents, local_tax_cents=d.local_tax_cents,
                email_subject=d.email_subject, email_body=d.email_body, pdf_available=bool(d.pdf_file),
                from_quote_id=d.from_quote_id, credit_for_id=d.credit_for_id, replaces_id=d.replaces_id)
    if d.is_draft:
        try:
            totals,ctx=svc.preview_totals(d)
            data['preview']=dict(subtotal_cents=totals.subtotal_cents, discount_cents=totals.discount_cents, tax_cents=totals.tax_cents, total_cents=totals.total_cents, tax_label=ctx.label, missing_location=ctx.missing)
        except BillingError:
            data['preview']=dict(available=False,reason='Tax configuration needs owner review before approval.')
    return data


def _lines(raw):
    lines=[]
    for item in raw:
        item=dict(item)
        if 'description' in item and not item['description'].strip():
            raise RuleError('Line descriptions cannot be blank.')
        product=get_object_or_404(Product, pk=item.pop('product_id')) if 'product_id' in item else None
        if product is not None:
            item['product']=product
        elif not item.get('description') or 'taxable' not in item:
            raise RuleError('Custom lines need a description and an explicit taxable flag.')
        lines.append(item)
    return lines


def _contact(data):
    if 'name' in data and not data['name'].strip():
        raise RuleError('A customer name is required.')
    if data.get('email'):
        try:
            validate_email(data['email'])
        except ValidationError as exc:
            raise RuleError('Invalid customer email.') from exc
    return data


def _editable(pk):
    doc=get_object_or_404(Document,pk=pk)
    if doc.source!='app':
        raise RuleError('Imported history is read-only.')
    return doc


@operation('list_customers','Find customers and their balances.',obj(dict(q=string(200),**PAGING)))
def list_customers(actor,args):
    qs=Customer.objects.select_related('tax_jurisdiction').order_by('pk')
    q=args.get('q','')
    if q:
        qs=qs.filter(Q(name__icontains=q)|Q(email__icontains=q)|Q(address__icontains=q))
    return page(qs,args,customer_json)


@operation('get_customer','Get contact information, confirmed tax location and balance.',obj(dict(customer_id=ID),['customer_id']))
def get_customer(actor,args):
    return customer_json(get_object_or_404(Customer,pk=args['customer_id']))


@operation('create_customer','Create contact information; cannot set tax exemption or confirm location.',obj(CONTACT,['name']),'create_customer')
def create_customer(actor,args):
    return customer_json(svc.create_customer(actor,**_contact(args)))


@operation('update_customer','Edit contact fields. An address change waits for owner tax confirmation.',obj(dict(customer_id=ID,**CONTACT),['customer_id']),'edit_customer_contact')
def update_customer(actor,args):
    args=dict(args)
    c=get_object_or_404(Customer,pk=args.pop('customer_id'))
    return customer_json(svc.update_customer(actor,c,**_contact(args)))


@operation('list_products','List active products and their default prices/taxability.',obj(PAGING))
def list_products(actor,args):
    return page(Product.objects.filter(active=True).order_by('pk'),args,lambda p:dict(id=p.pk,name=p.name,kind=p.kind,unit_cents=p.unit_price_cents,default_taxable=p.default_taxable))


@operation('list_tax_locations','List configured jurisdictions and current local rates; no rate changes.',obj(dict(q=string(100),**PAGING)))
def list_tax_locations(actor,args):
    from .models import LocalTaxRate
    qs=TaxJurisdiction.objects.filter(name__icontains=args.get('q','')).order_by('pk')
    return page(qs,args,lambda j:dict(id=j.pk,name=j.name,state=j.state,kind=j.kind,county=j.county,local_rate_bp=LocalTaxRate.bp_for(j,svc.today())))


@operation('propose_tax_location','Propose a customer location; only the owner can confirm it.',obj(dict(customer_id=ID,state=string(2,pattern='^[A-Z]{2}$'),jurisdiction_id=ID,address=string(4000),checked_on=string(10,format='date'),source=string(200,minLength=1)),['customer_id','state','checked_on','source']),'propose_tax_location')
def propose_location(actor,args):
    args=dict(args);c=get_object_or_404(Customer,pk=args.pop('customer_id'))
    jurisdiction=get_object_or_404(TaxJurisdiction,pk=args.pop('jurisdiction_id')) if 'jurisdiction_id' in args else None
    if jurisdiction and jurisdiction.state!=args['state']:
        raise RuleError('The jurisdiction must match the state.')
    args['checked_on']=dt.date.fromisoformat(args['checked_on'])
    prop=svc.propose_tax_location(actor,c,jurisdiction=jurisdiction,**args)
    return dict(id=prop.pk,status=prop.status,customer_id=c.pk)


@operation('list_documents','List documents, including overdue/due-soon filters.',obj(dict(kind=dict(enum=['invoice','quote','credit_note']),status=string(12),customer_id=ID,filter=dict(enum=['overdue','due_soon','draft']),**PAGING)))
def list_documents(actor,args):
    qs=Document.objects.select_related('customer').prefetch_related('lines').order_by('-pk')
    for key in ('kind','status','customer_id'):
        if key in args:
            qs=qs.filter(**{key:args[key]})
    mode=args.get('filter')
    if mode=='draft': qs=qs.filter(status='draft')
    elif mode in ('overdue','due_soon'):
        today=svc.today()
        qs=qs.filter(kind='invoice',status__in=['sent','approved','partly_paid'])
        qs=qs.filter(due_date__lt=today) if mode=='overdue' else qs.filter(due_date__gte=today,due_date__lte=today+dt.timedelta(days=14))
    return page(qs,args,document_json)


@operation('get_document','Read document status, content, payment links between records and frozen totals.',obj(dict(document_id=ID),['document_id']))
def get_document(actor,args):
    return document_json(get_object_or_404(Document,pk=args['document_id']))


@operation('create_draft','Create an unnumbered quote or invoice for owner review.',obj(dict(kind=dict(enum=['invoice','quote']),lines=LINES,**DRAFT_FIELDS),['kind','customer_id','lines']),'edit_draft')
def create_draft(actor,args):
    args=dict(args);kind=args.pop('kind');customer=get_object_or_404(Customer,pk=args.pop('customer_id'));lines=_lines(args.pop('lines'))
    return document_json(svc.create_draft(actor,kind,customer,lines,**args))


@operation('update_draft','Edit an existing draft, including email text. Approved content stays locked.',obj(dict(document_id=ID,lines=LINES,**DRAFT_FIELDS),['document_id']),'edit_draft')
def update_draft(actor,args):
    args=dict(args);doc=_editable(args.pop('document_id'))
    if 'customer_id' in args: args['customer']=get_object_or_404(Customer,pk=args.pop('customer_id'))
    if 'lines' in args: args['lines']=_lines(args['lines'])
    return document_json(svc.update_draft(actor,doc,**args))


@operation('preview_totals','Preview existing draft totals without approving or assigning a number.',obj(dict(document_id=ID),['document_id']))
def preview(actor,args):
    d=get_object_or_404(Document,pk=args['document_id'])
    if not d.is_draft: raise RuleError('Preview is for drafts; read frozen totals on approved documents.')
    totals,ctx=svc.preview_totals(d)
    return dict(subtotal_cents=totals.subtotal_cents,discount_cents=totals.discount_cents,taxable_cents=totals.taxable_cents,tax_cents=totals.tax_cents,total_cents=totals.total_cents,tax_label=ctx.label,missing_location=ctx.missing)


@operation('convert_quote','Convert an accepted quote into a linked draft invoice.',obj(dict(document_id=ID),['document_id']),'convert_quote')
def convert(actor,args):
    return document_json(svc.convert_quote(actor,_editable(args['document_id'])))


@operation('draft_late_fee','Draft a linked late-fee invoice only when the owner enabled it; never send.',obj(dict(document_id=ID),['document_id']),'draft_late_fee')
def late_fee(actor,args):
    d=svc.draft_late_fee(actor,_editable(args['document_id']))
    return document_json(d) if d else dict(created=False)


@operation('get_history','Read permanent document change history.',obj(dict(document_id=ID,**PAGING),['document_id']))
def history(actor,args):
    get_object_or_404(Document,pk=args['document_id'])
    return page(AuditEvent.objects.filter(entity='document',entity_id=str(args['document_id'])).order_by('pk'),args,
                lambda e:dict(id=e.pk,at=e.at,actor=e.actor,actor_ref=e.actor_ref,action=e.action,before=e.before,after=e.after,note=e.note))


@operation('list_payments','Read payments and pending refund decisions for a document.',obj(dict(document_id=ID,**PAGING),['document_id']))
def payments(actor,args):
    get_object_or_404(Document,pk=args['document_id'])
    return page(Payment.objects.filter(invoice_id=args['document_id']).order_by('pk'),args,lambda p:dict(id=p.pk,method=p.method,amount_cents=p.amount_cents,applied_cents=p.applied_cents,stripe_fee_cents=p.stripe_fee_cents,received_on=p.received_on,check_number=p.check_number,refunds=list(p.refunds.values('id','amount_cents','resolution'))))


@operation('list_receipts','Read receipt delivery status; cannot send or resend.',obj(dict(document_id=ID,**PAGING),['document_id']))
def receipts(actor,args):
    get_object_or_404(Document,pk=args['document_id'])
    return page(Receipt.objects.filter(payment__invoice_id=args['document_id']).order_by('pk'),args,lambda r:dict(id=r.pk,payment_id=r.payment_id,kind=r.kind,email_status=r.email_status,recipients=r.recipients,sent_at=r.sent_at,paid_copy_id=r.paid_copy_id,balance_after_cents=r.balance_after_cents))


@operation('upcoming_renewals','Read recurring templates due within the next 30 days. No scheduler runs.',obj(PAGING))
def renewals(actor,args):
    qs=RecurringTemplate.objects.filter(active=True,next_date__lte=svc.today()+dt.timedelta(days=30)).select_related('customer').order_by('next_date','pk')
    return page(qs,args,lambda r:dict(id=r.pk,customer_id=r.customer_id,customer_name=r.customer.name,next_date=r.next_date,interval=r.interval,lead_days=r.lead_days))


@operation('download_pdf','Download a stored original or PAID PDF. No rendering, approval or sending.',obj(dict(document_id=ID,paid_copy_id=ID),['document_id']))
def download(actor,args):
    doc=get_object_or_404(Document,pk=args['document_id'])
    source=get_object_or_404(PaidCopy,pk=args['paid_copy_id'],invoice=doc) if 'paid_copy_id' in args else doc
    data=delivery.stored_pdf(source.pdf_file,source.pdf_sha256)
    if len(data)>8*1024*1024: raise RuleError('PDF exceeds the private transport limit.')
    return dict(filename=f'{doc.display_number}{"-PAID" if "paid_copy_id" in args else ""}.pdf',mime_type='application/pdf',sha256=source.pdf_sha256,base64=base64.b64encode(data).decode())


def dispatch(actor,name,args):
    if actor.kind!=Actor.LEDGER:
        raise PermissionDenied('This interface accepts Ledger service tokens only.')
    if name not in HANDLERS:
        # Log known forbidden operations with the same capability checks as the UI.
        forbidden={'approve':'approve','send':'send','mark_sent':'send','void':'void','record_payment':'record_manual_payment','resend_receipt':'resend_receipt','update_settings':'edit_settings','confirm_tax_location':'confirm_tax_location','delete_draft':'delete_draft'}
        if name in forbidden: require(actor,forbidden[name],'api')
        audit.record(actor,'denied:api_operation','api','-',note='Unsupported operation')
        raise PermissionDenied('Operation is not exposed to Ledger.')
    handler,capability=HANDLERS[name]
    require(actor,capability,'api')
    if not isinstance(args,dict): raise RuleError('Arguments must be a JSON object.')
    sensitive=set(args)&(svc.TERMS_FIELDS|svc.CUSTOMER_TAX_FIELDS|{'actor','status','number','pdf_file','pdf_sha256'})
    if sensitive:
        audit.record(actor,'denied:api_fields','api','-',note='Protected fields refused')
        raise PermissionDenied('Protected fields cannot be changed by Ledger.')
    errors=list(StrictValidator(SPECS[name]['inputSchema'],format_checker=FormatChecker()).iter_errors(args))
    if errors:
        # Do not echo private arguments into error responses or logs.
        raise RuleError('Arguments do not match the operation schema: '+'.'.join(str(p) for p in errors[0].absolute_path))
    return audit._jsonable(handler(actor,args))
