"""Every change to billing data goes through these functions.

Pattern for each change: check permission (logs refusals) → open one transaction → lock the rows
involved → check state → change → write the audit entry → commit. If anything fails, nothing
happens, including number assignment.
"""

import datetime as dt
import hashlib
import secrets

from django.db import connection, transaction
from django.db.models import Sum
from django.utils import timezone

from . import audit, money
from .models import (
    Actor,
    ApiToken,
    BusinessSettings,
    Customer,
    CustomerCredit,
    Document,
    LineItem,
    NumberCounter,
    PaidCopy,
    Payment,
    Receipt,
    RecurringRun,
    RecurringTemplate,
    Refund,
    LocalTaxRate,
    StateTaxRate,
    StripeEvent,
    TaxJurisdiction,
    TaxLocationProposal,
)
from .permissions import ActorRef, LockedError, PermissionDenied, RuleError, allowed, require

D = Document
S = Document.Status
OPEN_INVOICE = (S.APPROVED, S.SENT, S.PARTLY_PAID, S.PAID)
STRIPE_MIN_CENTS = 50

CUSTOMER_CONTACT_FIELDS = {"name", "email", "phone", "address", "notes"}
CUSTOMER_TAX_FIELDS = {"tax_exempt", "exemption_note"}
DRAFT_FIELDS = {"notes", "email_subject", "email_body", "customer"}
TERMS_FIELDS = {"donated", "late_fee_enabled", "late_fee_bp", "tax_override_state", "tax_override_jurisdiction"}  # Blake only
HOME_STATE = "IA"


# --- snapshots for the change history ----------------------------------------


def customer_state(c):
    return {f: getattr(c, f) for f in ("name", "email", "phone", "address", "notes", "tax_exempt", "exemption_note")}


def document_state(doc):
    return {
        "kind": doc.kind,
        "status": doc.status,
        "number": doc.number,
        "customer_id": doc.customer_id,
        "issue_date": doc.issue_date,
        "due_date": doc.due_date,
        "donated": doc.donated,
        "late_fee_enabled": doc.late_fee_enabled,
        "late_fee_bp": doc.late_fee_bp,
        "notes": doc.notes,
        "subtotal_cents": doc.subtotal_cents,
        "discount_cents": doc.discount_cents,
        "tax_cents": doc.tax_cents,
        "total_cents": doc.total_cents,
        "sent_method": doc.sent_method,
        "lines": [
            {
                "description": li.description,
                "product_id": li.product_id,
                "qty_milli": li.qty_milli,
                "unit_cents": li.unit_cents,
                "discount_cents": li.discount_cents,
                "taxable": li.taxable,
                "tax_cents": li.tax_cents,
            }
            for li in doc.lines.all()
        ]
        if doc.pk
        else [],
    }


def _log(actor, action, obj, before=None, after=None, note=""):
    entity = obj._meta.model_name
    return audit.record(actor, action, entity, obj.pk, before=before, after=after, note=note)


def _deny(actor, action, obj, message, exc=LockedError):
    """Log a refused change (outside the failed transaction) and raise."""
    _log(actor, f"denied:{action}", obj, note=message)
    raise exc(message)


def today(now=None):
    return timezone.localdate(now or timezone.now())  # Central time (settings.TIME_ZONE)


# --- customers ----------------------------------------------------------------


def create_customer(actor: ActorRef, **fields):
    require(actor, "create_customer", "customer")
    if CUSTOMER_TAX_FIELDS & set(fields):
        require(actor, "edit_customer_tax", "customer")
    unknown = set(fields) - CUSTOMER_CONTACT_FIELDS - CUSTOMER_TAX_FIELDS
    if unknown:
        raise RuleError(f"unknown customer fields: {sorted(unknown)}")
    with transaction.atomic():
        c = Customer.objects.create(**fields)
        _log(actor, "create", c, after=customer_state(c))
    return c


def update_customer(actor: ActorRef, customer, **fields):
    require(actor, "edit_customer_contact", "customer", customer.pk)
    if CUSTOMER_TAX_FIELDS & set(fields):
        require(actor, "edit_customer_tax", "customer", customer.pk)
    unknown = set(fields) - CUSTOMER_CONTACT_FIELDS - CUSTOMER_TAX_FIELDS
    if unknown:
        raise RuleError(f"unknown customer fields: {sorted(unknown)}")
    with transaction.atomic():
        c = Customer.objects.select_for_update().get(pk=customer.pk)
        before = customer_state(c)
        address_changed = "address" in fields and fields["address"] != c.address
        for k, v in fields.items():
            setattr(c, k, v)
        if address_changed:
            c.tax_location_pending = True  # the confirmed location keeps applying until Blake confirms
        c.save()
        _log(
            actor,
            "update",
            c,
            before=before,
            after=customer_state(c),
            note="address changed: tax location pending Blake" if address_changed else "",
        )
    return c


# --- drafts -------------------------------------------------------------------


def _build_lines(actor, doc, lines, trust_taxable=False):
    """Validate line dicts and create LineItems. Ledger can't flip a product's taxable default,
    except when copying lines Blake already approved (trust_taxable)."""
    for i, data in enumerate(lines):
        data = dict(data)
        product = data.pop("product", None)
        taxable = data.pop("taxable", None)
        if product is not None:
            default = product.default_taxable
            if taxable is None:
                taxable = default
            elif taxable != default and not trust_taxable and not allowed(actor, "override_taxable"):
                raise PermissionDenied(f"line {i + 1}: only Blake can change whether {product.name} is taxed")
            data.setdefault("description", product.name)
            data.setdefault("unit_cents", product.unit_price_cents)
        if taxable is None:
            raise RuleError(f"line {i + 1}: say whether it's taxable")
        allowed_keys = {"description", "qty_milli", "unit_cents", "discount_cents", "discount_label"}
        unknown = set(data) - allowed_keys
        if unknown:
            raise RuleError(f"line {i + 1}: unknown fields {sorted(unknown)}")
        line = money.LineInput(
            unit_cents=data["unit_cents"],
            qty_milli=data.get("qty_milli", 1000),
            discount_cents=data.get("discount_cents", 0),
            taxable=bool(taxable),
        )  # validates integers, ranges, discount <= amount
        LineItem.objects.create(
            document=doc,
            position=i,
            product=product,
            taxable=line.taxable,
            line_cents=line.net_cents,
            **data,
        )


def _check_draft_fields(actor, fields, entity_id=""):
    unknown = set(fields) - DRAFT_FIELDS - TERMS_FIELDS
    if unknown:
        raise RuleError(f"unknown document fields: {sorted(unknown)}")
    if TERMS_FIELDS & set(fields):
        require(actor, "edit_billing_terms", "document", entity_id)


def _check_taxable_overrides(actor, lines, entity_id=""):
    """Refuse (and log, outside the transaction) a line that changes a product's taxable default."""
    for data in lines or []:
        product, taxable = data.get("product"), data.get("taxable")
        if product is not None and taxable is not None and taxable != product.default_taxable:
            require(actor, "override_taxable", "document", entity_id)


def create_draft(actor: ActorRef, kind, customer, lines, **fields):
    require(actor, "edit_draft", "document")
    if kind == D.Kind.CREDIT_NOTE:
        raise RuleError("credit notes are created from an invoice (create_credit_note_draft)")
    _check_draft_fields(actor, fields)
    _check_taxable_overrides(actor, lines)
    with transaction.atomic():
        doc = Document.objects.create(kind=kind, customer=customer, prepared_by=actor.kind, **fields)
        _build_lines(actor, doc, lines)
        _log(actor, "create_draft", doc, after=document_state(doc))
    return doc


def _lock_draft(actor, doc, action):
    """Lock the row; refuse (and log) if it is no longer a draft."""
    locked = Document.objects.select_for_update().get(pk=doc.pk)
    if locked.status != S.DRAFT:
        return None
    return locked


def update_draft(actor: ActorRef, doc, lines=None, **fields):
    require(actor, "edit_draft", "document", doc.pk)
    _check_draft_fields(actor, fields, doc.pk)
    _check_taxable_overrides(actor, lines, doc.pk)
    with transaction.atomic():
        d = _lock_draft(actor, doc, "edit")
        if d is not None:
            before = document_state(d)
            for k, v in fields.items():
                setattr(d, k, v)
            d.save()
            if lines is not None:
                d.lines.all().delete()
                _build_lines(actor, d, lines)
            _log(actor, "update_draft", d, before=before, after=document_state(d))
            return d
    _deny(actor, "edit", doc, f"{doc} is approved and locked; void it or issue a credit note instead")


def delete_draft(actor: ActorRef, doc):
    require(actor, "delete_draft", "document", doc.pk)
    with transaction.atomic():
        d = _lock_draft(actor, doc, "delete")
        if d is not None:
            before = document_state(d)
            _log(actor, "delete_draft", d, before=before)
            d.delete()
            return
    _deny(actor, "delete", doc, f"{doc} is not a draft and can't be deleted")


# --- tax location -------------------------------------------------------------

from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class TaxContext:
    state: str = ""
    jurisdiction: object = None
    state_bp: int = 0
    local_bp: int = 0
    out_of_state: bool = False
    missing: bool = False  # no tax location, and a taxed line needs one

    @property
    def label(self):
        if self.out_of_state:
            return "No Iowa sales tax (out of state)"
        if not self.state:
            return ""
        total = money.format_rate(self.state_bp + self.local_bp)
        if self.local_bp and self.jurisdiction is not None:
            return f"Sales tax ({total}, Iowa + {self.jurisdiction.name})"
        return f"Sales tax ({total}, Iowa)"


MISSING_LOCATION = "Add the client's address first: a taxed line needs the client's tax location."


def tax_context(doc, customer, day, lines):
    """Which rates apply (spec v0.6 §4): Iowa state rate + the local option where the client is.
    Credit notes reuse their invoice's frozen rates, so tax is credited at the original rate."""
    if doc.kind == D.Kind.CREDIT_NOTE and doc.credit_for_id:
        inv = Document.objects.get(pk=doc.credit_for_id)
        return TaxContext(inv.tax_state, inv.tax_jurisdiction, inv.state_rate_bp or 0, inv.local_rate_bp or 0, inv.out_of_state)
    needs_tax = any(li.taxable for li in lines) and not customer.tax_exempt and not doc.donated
    if doc.tax_override_state:
        state, jurisdiction = doc.tax_override_state, doc.tax_override_jurisdiction
    elif customer.tax_state:
        state, jurisdiction = customer.tax_state, customer.tax_jurisdiction
    else:
        return TaxContext(missing=needs_tax)
    if state != HOME_STATE:
        return TaxContext(state=state, out_of_state=True)
    if jurisdiction is None:
        return TaxContext(missing=needs_tax)
    try:
        state_bp = StateTaxRate.for_date(HOME_STATE, day).rate_bp
    except LookupError as e:
        raise RuleError(str(e)) from e
    return TaxContext(state, jurisdiction, state_bp, LocalTaxRate.bp_for(jurisdiction, day))


def _line_inputs(lines):
    return [money.LineInput(li.unit_cents, li.qty_milli, li.discount_cents, li.taxable) for li in lines]


def preview_totals(doc, on_date=None):
    """Live totals for the editor and the API. Same function approval uses.
    Returns (totals, context); context.missing means the editor shows the address warning."""
    lines = list(doc.lines.all())
    ctx = tax_context(doc, doc.customer, on_date or today(), lines)
    totals = money.compute_totals(_line_inputs(lines), ctx.state_bp, ctx.local_bp, customer_exempt=doc.customer.tax_exempt, donated=doc.donated)
    return totals, ctx


def _get_jurisdiction(jurisdiction):
    if jurisdiction is None or isinstance(jurisdiction, TaxJurisdiction):
        return jurisdiction
    return TaxJurisdiction.objects.get(pk=jurisdiction)


def propose_tax_location(actor: ActorRef, customer, state, jurisdiction=None, address="", checked_on=None, source=""):
    """Ledger or Blake proposes; nothing applies until Blake confirms."""
    require(actor, "propose_tax_location", "customer", customer.pk)
    state = (state or "").upper()
    jurisdiction = _get_jurisdiction(jurisdiction)
    if state == HOME_STATE and jurisdiction is None:
        raise RuleError("an Iowa location needs its local jurisdiction (city or rural county)")
    with transaction.atomic():
        prop = TaxLocationProposal.objects.create(
            customer=customer, state=state, jurisdiction=jurisdiction, address=address, checked_on=checked_on, source=source, proposed_by=actor.kind
        )
        Customer.objects.filter(pk=customer.pk).update(tax_location_pending=True)
        _log(actor, "propose_tax_location", customer, after={"proposal_id": prop.pk, "state": state, "jurisdiction": getattr(jurisdiction, "name", "")})
    return prop


def confirm_tax_location(actor: ActorRef, customer, state=None, jurisdiction=None, checked_on=None, source="", proposal=None):
    """Blake confirms a tax location, either a proposal or values he enters."""
    require(actor, "confirm_tax_location", "customer", customer.pk)
    with transaction.atomic():
        c = Customer.objects.select_for_update().get(pk=customer.pk)
        if proposal is not None:
            prop = TaxLocationProposal.objects.select_for_update().get(pk=proposal.pk, customer=c)
            if prop.status != TaxLocationProposal.Status.PENDING:
                raise RuleError("this proposal was already decided")
            state, jurisdiction, checked_on, source = prop.state, prop.jurisdiction, prop.checked_on, prop.source
            if prop.address:
                c.address = prop.address
        state = (state or "").upper()
        jurisdiction = _get_jurisdiction(jurisdiction)
        if len(state) != 2:
            raise RuleError("give the two-letter state")
        if state == HOME_STATE and jurisdiction is None:
            raise RuleError("an Iowa location needs its local jurisdiction (city or rural county)")
        before = {**customer_state(c), **_location_state(c)}
        c.tax_state, c.tax_jurisdiction = state, jurisdiction if state == HOME_STATE else None
        c.tax_location_checked_on, c.tax_location_source = checked_on or today(), source
        c.tax_location_confirmed_at, c.tax_location_pending = timezone.now(), False
        c.save()
        if proposal is not None:
            TaxLocationProposal.objects.filter(pk=prop.pk).update(status=TaxLocationProposal.Status.CONFIRMED, decided_at=timezone.now())
        TaxLocationProposal.objects.filter(customer=c, status=TaxLocationProposal.Status.PENDING).exclude(pk=getattr(proposal, "pk", None)).update(
            status=TaxLocationProposal.Status.REJECTED, decided_at=timezone.now()
        )
        _log(actor, "confirm_tax_location", c, before=before, after={**customer_state(c), **_location_state(c)})
    return c


def _location_state(c):
    return {
        "tax_state": c.tax_state,
        "tax_jurisdiction": getattr(c.tax_jurisdiction, "name", ""),
        "tax_location_checked_on": c.tax_location_checked_on,
        "tax_location_pending": c.tax_location_pending,
    }


# --- tax report -----------------------------------------------------------------


def tax_report(start, end):
    """Sales tax by local jurisdiction for issued invoices and credit notes dated start..end (Central),
    plus exempt, non-taxable and out-of-state sales, and tax received from payments in the period."""
    rows, exempt, non_taxable, out_of_state = {}, {}, 0, 0
    docs = (
        Document.objects.filter(kind__in=[D.Kind.INVOICE, D.Kind.CREDIT_NOTE], issue_date__gte=start, issue_date__lte=end)
        .exclude(status__in=[S.DRAFT, S.VOID])
        .exclude(source="stripe_import")
        .select_related("tax_jurisdiction")
    )
    totals = {"taxable_cents": 0, "state_tax_cents": 0, "local_tax_cents": 0, "tax_cents": 0}
    for d in docs:
        sign = -1 if d.kind == D.Kind.CREDIT_NOTE else 1
        sales = d.subtotal_cents - d.discount_cents
        snap = d.customer_snapshot or {}
        if d.donated:
            continue  # nothing sold
        if snap.get("tax_exempt"):
            exempt[snap.get("name", "?")] = exempt.get(snap.get("name", "?"), 0) + sign * sales
            continue
        if d.out_of_state:
            out_of_state += sign * sales
            continue
        non_taxable += sign * (sales - d.taxable_cents)
        if d.taxable_cents:
            key = d.tax_jurisdiction.name if d.tax_jurisdiction else "Iowa (no local option)"
            row = rows.setdefault(key, {"jurisdiction": key, "local_rate_bp": d.local_rate_bp or 0, "taxable_cents": 0, "state_tax_cents": 0, "local_tax_cents": 0})
            row["taxable_cents"] += sign * d.taxable_cents
            row["state_tax_cents"] += sign * d.state_tax_cents
            row["local_tax_cents"] += sign * d.local_tax_cents
            totals["taxable_cents"] += sign * d.taxable_cents
            totals["state_tax_cents"] += sign * d.state_tax_cents
            totals["local_tax_cents"] += sign * d.local_tax_cents
            totals["tax_cents"] += sign * d.tax_cents

    received = {"tax_cents": 0, "state_tax_cents": 0, "local_tax_cents": 0, "other_cents": 0}
    for p in Payment.objects.filter(received_on__gte=start, received_on__lte=end).select_related("invoice"):
        inv = p.invoice
        tax_part, other = money.split_payment_tax(p.applied_cents, inv.tax_cents, inv.total_cents)
        state_part = money.div_round_half_up(tax_part * inv.state_tax_cents, inv.tax_cents) if inv.tax_cents else 0
        received["tax_cents"] += tax_part
        received["state_tax_cents"] += state_part
        received["local_tax_cents"] += tax_part - state_part
        received["other_cents"] += other
    return {
        "start": start,
        "end": end,
        "jurisdictions": sorted(rows.values(), key=lambda r: r["jurisdiction"]),
        "exempt_sales": [{"customer": k, "sales_cents": v} for k, v in sorted(exempt.items())],
        "non_taxable_cents": non_taxable,
        "out_of_state_cents": out_of_state,
        "invoiced": totals,
        "received": received,
    }


# --- numbering + approval -----------------------------------------------------


def _next_number(series, year):
    """Take the next number for (series, year). Must run inside the approval transaction: the row
    lock makes concurrent approvals wait, and a rollback gives the number back."""
    with connection.cursor() as cur:
        cur.execute(
            f"INSERT INTO {NumberCounter._meta.db_table} (series, year, next_seq) VALUES (%s, %s, 1) "
            "ON CONFLICT (series, year) DO NOTHING",
            [series, year],
        )
        cur.execute(
            f"SELECT id, next_seq FROM {NumberCounter._meta.db_table} WHERE series = %s AND year = %s FOR UPDATE",
            [series, year],
        )
        counter_id, seq = cur.fetchone()
        cur.execute(f"UPDATE {NumberCounter._meta.db_table} SET next_seq = next_seq + 1 WHERE id = %s", [counter_id])
    return seq


def format_number(series, year, seq):
    return f"{series}-{year}-{seq:04d}"


def approve(actor: ActorRef, doc, now=None, render_pdf=None):
    """Blake approves: freeze lines, totals, customer and tax rate; assign the number; make the PDF.
    `render_pdf(doc) -> (path, sha256)` runs inside the transaction; if it fails, nothing is used up."""
    require(actor, "approve", "document", doc.pk)
    now = now or timezone.now()
    with transaction.atomic():
        d = _lock_draft(actor, doc, "approve")
        if d is not None:
            lines = list(d.lines.all())
            if not lines:
                raise RuleError("add at least one line before approving")
            day = today(now)
            settings = BusinessSettings.load()
            customer = Customer.objects.select_for_update().get(pk=d.customer_id)
            ctx = tax_context(d, customer, day, lines)
            if ctx.missing:
                raise RuleError(MISSING_LOCATION)  # before any number is taken
            before = document_state(d)
            totals = money.compute_totals(_line_inputs(lines), ctx.state_bp, ctx.local_bp, customer_exempt=customer.tax_exempt, donated=d.donated)
            if d.kind == D.Kind.CREDIT_NOTE:
                _check_credit_note_amount(d, totals.total_cents)
            for li, res in zip(lines, totals.lines):
                li.line_cents, li.tax_rate_bp, li.tax_cents = res.net_cents, res.tax_rate_bp, res.tax_cents
                li.state_rate_bp, li.state_tax_cents = res.state_rate_bp, res.state_tax_cents
                li.local_rate_bp, li.local_tax_cents = res.local_rate_bp, res.local_tax_cents
                li.tax_jurisdiction = ctx.jurisdiction if res.taxable else None
                li.save()

            series = D.SERIES[d.kind]
            seq = _next_number(series, day.year)
            d.series, d.year, d.seq = series, day.year, seq
            d.number = format_number(series, day.year, seq)
            d.issue_date = day
            d.due_date = day + dt.timedelta(days=settings.net_days) if d.kind == D.Kind.INVOICE else None
            d.tax_state, d.tax_jurisdiction, d.out_of_state = ctx.state, ctx.jurisdiction, ctx.out_of_state
            d.state_rate_bp, d.local_rate_bp, d.tax_rate_bp = ctx.state_bp, ctx.local_bp, ctx.state_bp + ctx.local_bp
            d.tax_label = ctx.label if totals.tax_cents or ctx.out_of_state else ""
            d.state_tax_cents, d.local_tax_cents = totals.state_tax_cents, totals.local_tax_cents
            d.customer_snapshot = audit._jsonable({**customer_state(customer), **_location_state(customer), "id": customer.pk})
            d.business_snapshot = {
                "name": settings.name,
                "address_line1": settings.address_line1,
                "city_state_zip": settings.city_state_zip,
                "phone": settings.phone,
                "email": settings.email,
            }
            d.subtotal_cents, d.discount_cents = totals.subtotal_cents, totals.discount_cents
            d.taxable_cents, d.tax_cents = totals.taxable_cents, totals.tax_cents
            d.value_cents, d.total_cents = totals.value_cents, totals.total_cents
            d.approved_at = now
            if d.kind == D.Kind.INVOICE and not d.donated:
                d.pay_code = secrets.token_urlsafe(12)
            d.status = S.APPROVED
            if render_pdf is not None:
                d.pdf_file, d.pdf_sha256 = render_pdf(d)
            d.save()  # one write: the lock trigger allows it because the row was still a draft
            _log(actor, "approve", d, before=before, after=document_state(d))
            if d.kind == D.Kind.CREDIT_NOTE:
                _apply_credit_note(actor, d)
            return d
    _deny(actor, "approve", doc, f"{doc} is not a draft")


# --- sending ------------------------------------------------------------------


def mark_sent(actor: ActorRef, doc, method="manual", recipients=None, now=None):
    """Record that Blake sent it (by email from the app, or himself after downloading).
    Returns a list of warnings (e.g. sent long after approval)."""
    require(actor, "send", "document", doc.pk)
    if method not in ("manual", "email"):
        raise RuleError("method must be 'manual' or 'email'")
    now = now or timezone.now()
    warnings = []
    with transaction.atomic():
        d = Document.objects.select_for_update().get(pk=doc.pk)
        if d.status in (S.DRAFT, S.VOID) or d.status in (S.DECLINED, S.EXPIRED, S.CONVERTED):
            raise RuleError(f"{d} can't be sent in its current state ({d.get_status_display()})")
        before = document_state(d)
        days = (today(now) - d.issue_date).days
        limit = BusinessSettings.load().send_warning_days
        if days > limit:
            warnings.append(f"Sent {days} days after approval; the issue and due dates stay as approved.")
        if d.sent_at is None:
            d.sent_at = now
            d.sent_method = method
        if d.status == S.APPROVED:
            d.status = S.SENT
        d.save()
        if d.kind == D.Kind.INVOICE:
            _refresh_invoice_status(d)
        _log(
            actor,
            "sent_email" if method == "email" else "sent_manually",
            d,
            before=before,
            after={**document_state(d), "recipients": recipients or []},
            note="sent manually" if method == "manual" else f"emailed to {', '.join(recipients or [])}",
        )
    return warnings


# --- void / replace / quotes --------------------------------------------------


def void(actor: ActorRef, doc, reason):
    require(actor, "void", "document", doc.pk)
    if not reason:
        raise RuleError("give a reason for voiding")
    with transaction.atomic():
        d = Document.objects.select_for_update().get(pk=doc.pk)
        if d.kind == D.Kind.QUOTE:
            raise RuleError("quotes are declined or expire; they aren't voided")
        if d.status in (S.DRAFT, S.VOID):
            raise RuleError("only approved documents can be voided (delete a draft instead)")
        if d.kind == D.Kind.INVOICE and (_applied_total(d) > 0 or _credit_notes_total(d) > 0):
            raise RuleError("this invoice has payments or credits; issue a credit note instead of voiding")
        before = document_state(d)
        d.status, d.voided_at, d.void_reason = S.VOID, timezone.now(), reason
        d.save()
        _log(actor, "void", d, before=before, after=document_state(d), note=reason)
    return d


def create_replacement(actor: ActorRef, voided):
    """Start a corrected copy of a voided invoice, linked to it."""
    require(actor, "edit_draft", "document", voided.pk)
    voided = Document.objects.get(pk=voided.pk)
    if voided.status != S.VOID:
        raise RuleError("only a voided document can be replaced")
    lines = [
        {
            "description": li.description,
            "product": li.product,
            "qty_milli": li.qty_milli,
            "unit_cents": li.unit_cents,
            "discount_cents": li.discount_cents,
            "discount_label": li.discount_label,
            "taxable": li.taxable,
        }
        for li in voided.lines.all()
    ]
    with transaction.atomic():
        new = Document.objects.create(
            kind=voided.kind,
            customer=voided.customer,
            prepared_by=actor.kind,
            notes=voided.notes,
            donated=voided.donated,
            late_fee_enabled=voided.late_fee_enabled,
            late_fee_bp=voided.late_fee_bp,
            replaces=voided,
        )
        # Copying an approved line keeps its taxable flag, even for Ledger: Blake approved it.
        _build_lines(actor, new, lines, trust_taxable=True)
        _log(actor, "create_replacement", new, after=document_state(new), note=f"replaces {voided.number}")
    return new


def set_quote_outcome(actor: ActorRef, quote, outcome):
    require(actor, "quote_outcome", "document", quote.pk)
    if outcome not in (S.ACCEPTED, S.DECLINED, S.EXPIRED):
        raise RuleError("outcome must be accepted, declined or expired")
    with transaction.atomic():
        q = Document.objects.select_for_update().get(pk=quote.pk)
        if q.kind != D.Kind.QUOTE or q.status not in (S.APPROVED, S.SENT):
            raise RuleError("only an approved or sent quote can be accepted, declined or expire")
        before = document_state(q)
        q.status = outcome
        q.save()
        _log(actor, f"quote_{outcome}", q, before=before, after=document_state(q))
    return q


def convert_quote(actor: ActorRef, quote):
    require(actor, "convert_quote", "document", quote.pk)
    with transaction.atomic():
        q = Document.objects.select_for_update().get(pk=quote.pk)
        if q.kind != D.Kind.QUOTE or q.status != S.ACCEPTED:
            raise RuleError("only an accepted quote can be converted")
        inv = Document.objects.create(kind=D.Kind.INVOICE, customer=q.customer, prepared_by=actor.kind, notes=q.notes, from_quote=q)
        for li in q.lines.all():
            LineItem.objects.create(
                document=inv,
                position=li.position,
                product=li.product,
                description=li.description,
                qty_milli=li.qty_milli,
                unit_cents=li.unit_cents,
                discount_cents=li.discount_cents,
                discount_label=li.discount_label,
                taxable=li.taxable,
                line_cents=li.line_cents,
            )
        before = document_state(q)
        q.status = S.CONVERTED
        q.save()
        _log(actor, "convert", q, before=before, after=document_state(q), note=f"draft invoice #{inv.pk}")
        _log(actor, "create_from_quote", inv, after=document_state(inv), note=f"from {q.number}")
    return inv


# --- credit notes -------------------------------------------------------------


def create_credit_note_draft(actor: ActorRef, invoice, lines, notes=""):
    require(actor, "credit_note", "document", invoice.pk)
    invoice = Document.objects.get(pk=invoice.pk)
    if invoice.kind != D.Kind.INVOICE or invoice.status not in OPEN_INVOICE:
        raise RuleError("credit notes are for approved invoices")
    with transaction.atomic():
        cn = Document.objects.create(kind=D.Kind.CREDIT_NOTE, customer=invoice.customer, prepared_by=actor.kind, credit_for=invoice, notes=notes)
        _build_lines(actor, cn, lines)
        _log(actor, "create_draft", cn, after=document_state(cn), note=f"credit for {invoice.number}")
    return cn


def _check_credit_note_amount(cn, total):
    inv = Document.objects.select_for_update().get(pk=cn.credit_for_id)
    if inv.status not in OPEN_INVOICE:
        raise RuleError("the invoice must be approved and not void")
    if total > inv.total_cents - _credit_notes_total(inv):
        raise RuleError("credit is more than what's left on the invoice")


def _apply_credit_note(actor, cn):
    inv = Document.objects.select_for_update().get(pk=cn.credit_for_id)
    _refresh_invoice_status(inv, actor=actor, note=f"credit note {cn.number}")


# --- balances -----------------------------------------------------------------


def _applied_total(inv):
    return inv.payments.aggregate(s=Sum("applied_cents"))["s"] or 0


def _credit_notes_total(inv):
    return (
        Document.objects.filter(credit_for=inv, kind=D.Kind.CREDIT_NOTE)
        .exclude(status__in=[S.DRAFT, S.VOID])
        .aggregate(s=Sum("total_cents"))["s"]
        or 0
    )


def _decided_refunds_total(inv):
    return (
        Refund.objects.filter(payment__invoice=inv)
        .exclude(resolution=Refund.Resolution.PENDING)
        .aggregate(s=Sum("amount_cents"))["s"]
        or 0
    )


def balance_cents(inv) -> int:
    """What the client still owes. A refund only changes this once Blake decides (spec test 18)."""
    if inv.kind != D.Kind.INVOICE or inv.status in (S.DRAFT, S.VOID):
        return 0
    return inv.total_cents - _applied_total(inv) - _credit_notes_total(inv) + _decided_refunds_total(inv)


def _refresh_invoice_status(inv, actor=None, note=""):
    if inv.status not in OPEN_INVOICE:
        return
    bal = balance_cents(inv)
    if bal < 0:
        # A credit note on an already-paid invoice: the excess becomes customer credit, logged.
        CustomerCredit.objects.create(customer=inv.customer, amount_cents=-bal, note=f"over-credited {inv.number}")
        audit.record(actor or ActorRef(Actor.SYSTEM), "customer_credit", "customer", inv.customer_id, after={"amount_cents": -bal}, note=note or inv.number)
        bal = 0
    settled_something = _applied_total(inv) > 0 or _credit_notes_total(inv) > 0
    if bal == 0:
        # A $0 (donated) invoice counts as paid once it has been sent.
        new = S.PAID if (settled_something or inv.sent_at) else S.APPROVED
    elif bal < inv.total_cents:
        new = S.PARTLY_PAID
    else:
        new = S.SENT if inv.sent_at else S.APPROVED
    if new != inv.status:
        old = inv.status
        Document.objects.filter(pk=inv.pk).update(status=new)
        inv.status = new
        if old == S.PAID:
            _supersede_paid_copy(inv, actor, note or "balance reopened")


def badges(inv, on_date=None):
    """Status badges for screens. Overdue is shown alongside Sent / Partly paid (spec §5)."""
    labels = [inv.get_status_display()]
    if inv.kind == D.Kind.INVOICE and inv.status in (S.APPROVED, S.SENT, S.PARTLY_PAID) and inv.due_date:
        if (on_date or today()) > inv.due_date and balance_cents(inv) > 0:
            labels.append("Overdue")
    if inv.status == S.DRAFT and inv.prepared_by == Actor.LEDGER:
        labels.append("Prepared by Ledger — needs review")
    if inv.donated:
        labels.append("Donated")
    return labels


def can_pay_online(inv):
    # Stripe can't charge under $0.50: below that the pay link is off and the page says "Contact us".
    return inv.kind == D.Kind.INVOICE and inv.status in (S.SENT, S.PARTLY_PAID) and balance_cents(inv) >= STRIPE_MIN_CENTS and not inv.donated


# --- payments -----------------------------------------------------------------


def _paid_copy_items(inv):
    items = [
        {"type": "payment", "date": p.received_on, "method": p.method, "check_number": p.check_number, "amount_cents": p.applied_cents}
        for p in inv.payments.order_by("received_on", "pk")
        if p.applied_cents
    ]
    items += [
        {"type": "credit_note", "date": cn.issue_date, "number": cn.number, "amount_cents": cn.total_cents}
        for cn in Document.objects.filter(credit_for=inv, kind=D.Kind.CREDIT_NOTE).exclude(status__in=[S.DRAFT, S.VOID]).order_by("pk")
    ]
    refunds = Refund.objects.filter(payment__invoice=inv).exclude(resolution=Refund.Resolution.PENDING)
    items += [{"type": "refund", "date": timezone.localdate(r.created_at), "amount_cents": -r.amount_cents} for r in refunds.order_by("pk")]
    return items


def _current_paid_copy(inv, paid_on):
    """The stored PAID copy for this paid-in-full moment; made once."""
    copy = PaidCopy.objects.filter(invoice=inv, superseded_at__isnull=True).first()
    if copy is None:
        copy = PaidCopy.objects.create(invoice=inv, paid_in_full_on=paid_on, applied=audit._jsonable(_paid_copy_items(inv)))
        audit.record(ActorRef(Actor.SYSTEM), "paid_copy_created", "document", inv.pk, after={"paid_copy_id": copy.pk, "paid_in_full_on": paid_on})
    return copy


def _supersede_paid_copy(inv, actor, reason):
    copy = PaidCopy.objects.filter(invoice=inv, superseded_at__isnull=True).first()
    if copy is not None:
        PaidCopy.objects.filter(pk=copy.pk).update(superseded_at=timezone.now(), superseded_reason=reason[:300])
        audit.record(actor or ActorRef(Actor.SYSTEM), "paid_copy_superseded", "document", inv.pk, after={"paid_copy_id": copy.pk}, note=reason)


def _apply_payment(inv, actor, method, amount_cents, received_on, email_receipt=True, **extra):
    bal = max(balance_cents(inv), 0)
    applied = min(amount_cents, bal)
    p = Payment.objects.create(
        invoice=inv,
        method=method,
        amount_cents=amount_cents,
        applied_cents=applied,
        received_on=received_on,
        recorded_by=actor.kind,
        **extra,
    )
    excess = amount_cents - applied
    if excess:
        CustomerCredit.objects.create(customer=inv.customer, amount_cents=excess, source_payment=p, note=f"overpaid {inv.number}")
    before_status = inv.status
    _refresh_invoice_status(inv)
    audit.record(
        actor,
        "payment",
        "document",
        inv.pk,
        before={"status": before_status, "balance_cents": bal},
        after={
            "status": inv.status,
            "balance_cents": balance_cents(inv),
            "payment": {"id": p.pk, "method": method, "amount_cents": amount_cents, "applied_cents": applied, "credit_cents": excess},
        },
    )
    _create_receipt(inv, p, actor, excess, email_receipt)
    return p


def _create_receipt(inv, payment, actor, credit_cents, email_receipt):
    """One receipt per payment, in the payment's transaction. Emailing happens later (deliver_receipt)."""
    paid_in_full = inv.status == S.PAID
    copy = _current_paid_copy(inv, payment.received_on) if paid_in_full else None
    r = Receipt.objects.create(
        payment=payment,
        kind=Receipt.Kind.PAID_IN_FULL if paid_in_full else Receipt.Kind.PARTIAL,
        paid_copy=copy,
        paid_so_far_cents=_applied_total(inv),
        balance_after_cents=max(balance_cents(inv), 0),
        credit_cents=credit_cents,
        email_status=Receipt.EmailStatus.PENDING if email_receipt else Receipt.EmailStatus.NOT_SENT,
        recipients=[inv.customer.email] if inv.customer.email else [],
    )
    if not email_receipt:
        audit.record(actor, "receipt_not_sent", "document", inv.pk, after={"receipt_id": r.pk, "payment_id": payment.pk}, note="receipt not sent")
    return r


def receipt_summary(receipt):
    """What the receipt email says (spec v0.6 §6)."""
    p = receipt.payment
    inv = p.invoice
    method = {
        Payment.Method.STRIPE: "Card/bank (online)",
        Payment.Method.CHECK: f"Check #{p.check_number}" if p.check_number else "Check",
        Payment.Method.CASH: "Cash",
    }.get(p.method, "Other")
    return {
        "from": BusinessSettings.load().email,
        "to": list(receipt.recipients),
        "invoice_number": inv.number,
        "payment_date": p.received_on,
        "amount": money.format_usd(p.amount_cents),
        "method": method,
        "paid_so_far": money.format_usd(receipt.paid_so_far_cents),
        "balance": "Paid in full" if receipt.kind == Receipt.Kind.PAID_IN_FULL else money.format_usd(receipt.balance_after_cents),
        "credit": money.format_usd(receipt.credit_cents) if receipt.credit_cents else "",
        "attach_paid_copy": receipt.kind == Receipt.Kind.PAID_IN_FULL,
    }


def deliver_receipt(receipt, render_paid_copy, send_email, now=None):
    """Email one receipt, exactly once. `render_paid_copy(copy) -> (path, sha256)` makes the stored
    PAID copy the first time; `send_email(receipt, summary, paid_copy)` raises on failure.
    A failure is recorded for Blake and never touches the payment."""
    with transaction.atomic():
        r = Receipt.objects.select_for_update().get(pk=receipt.pk)
        if r.email_status != Receipt.EmailStatus.PENDING:
            return r
        r.attempts += 1
        copy = None
        try:
            if not r.recipients:
                raise RuntimeError("the client has no email address")
            if r.paid_copy_id:
                copy = PaidCopy.objects.select_for_update().get(pk=r.paid_copy_id)
                if not copy.pdf_file:
                    copy.pdf_file, copy.pdf_sha256 = render_paid_copy(copy)
                    copy.save(update_fields=["pdf_file", "pdf_sha256"])
            send_email(r, receipt_summary(r), copy)
        except Exception as e:
            r.email_status, r.last_error = Receipt.EmailStatus.FAILED, str(e)[:300]
            r.save(update_fields=["email_status", "last_error", "attempts"])
            audit.record(ActorRef(Actor.SYSTEM), "receipt_failed", "document", r.payment.invoice_id, after={"receipt_id": r.pk}, note=r.last_error)
            return r
        r.email_status, r.sent_at, r.last_error = Receipt.EmailStatus.SENT, now or timezone.now(), ""
        r.save(update_fields=["email_status", "sent_at", "last_error", "attempts"])
        audit.record(
            ActorRef(Actor.SYSTEM),
            "receipt_sent",
            "document",
            r.payment.invoice_id,
            after={"receipt_id": r.pk, "payment_id": r.payment_id, "to": r.recipients, "paid_copy_id": r.paid_copy_id},
        )
        return r


def resend_receipt(actor: ActorRef, receipt, render_paid_copy, send_email):
    """Blake only. Sends the same receipt again, with the same stored PAID copy."""
    require(actor, "resend_receipt", "receipt", receipt.pk)
    with transaction.atomic():
        r = Receipt.objects.select_for_update().get(pk=receipt.pk)
        if r.email_status == Receipt.EmailStatus.PENDING:
            raise RuleError("this receipt is already waiting to be sent")
        Receipt.objects.filter(pk=r.pk).update(email_status=Receipt.EmailStatus.PENDING)
        audit.record(actor, "receipt_resend", "document", r.payment.invoice_id, after={"receipt_id": r.pk})
    return deliver_receipt(r, render_paid_copy, send_email)


# --- settings -----------------------------------------------------------------

SETTINGS_FIELDS = {"name", "address_line1", "city_state_zip", "phone", "email", "net_days", "min_online_payment_cents", "late_fee_default_bp", "send_warning_days"}


def update_settings(actor: ActorRef, **fields):
    require(actor, "edit_settings", "settings", 1)
    unknown = set(fields) - SETTINGS_FIELDS
    if unknown:
        raise RuleError(f"unknown settings: {sorted(unknown)}")
    if "min_online_payment_cents" in fields:
        money._require_int("min_online_payment_cents", fields["min_online_payment_cents"])
        if fields["min_online_payment_cents"] < 50:
            raise RuleError("Stripe can't charge less than $0.50")
    with transaction.atomic():
        st = BusinessSettings.objects.select_for_update().get_or_create(pk=1)[0]
        before = {f: getattr(st, f) for f in SETTINGS_FIELDS}
        for k, v in fields.items():
            setattr(st, k, v)
        st.save()
        audit.record(actor, "update_settings", "settings", 1, before=before, after={f: getattr(st, f) for f in SETTINGS_FIELDS})
    return st


def record_manual_payment(actor: ActorRef, invoice, method, amount_cents, received_on, check_number="", note="", email_receipt=True):
    require(actor, "record_manual_payment", "document", invoice.pk)
    if method not in (Payment.Method.CHECK, Payment.Method.CASH, Payment.Method.OTHER):
        raise RuleError("manual payments are check, cash or other; online payments come only from Stripe")
    money._require_int("amount_cents", amount_cents)
    if amount_cents <= 0:
        raise RuleError("amount must be positive")
    with transaction.atomic():
        inv = Document.objects.select_for_update().get(pk=invoice.pk)
        if inv.kind != D.Kind.INVOICE or inv.status not in OPEN_INVOICE:
            raise RuleError("payments can only be recorded on approved invoices")
        return _apply_payment(inv, actor, method, amount_cents, received_on, email_receipt=email_receipt, check_number=check_number, note=note)


def check_online_amount(invoice, amount_cents):
    """Server-side check before a Checkout session is created (the browser's number is never trusted)."""
    inv = Document.objects.get(pk=invoice.pk)
    if not can_pay_online(inv):
        raise RuleError("this invoice can't be paid online")
    try:
        money.validate_online_amount(amount_cents, balance_cents(inv), BusinessSettings.load().min_online_payment_cents)
    except (ValueError, TypeError) as e:
        raise RuleError(str(e)) from e


def _claim_event(event_id, event_type, payload):
    """Store a verified Stripe event once. Returns False if we've seen it before."""
    with connection.cursor() as cur:
        cur.execute(
            f"INSERT INTO {StripeEvent._meta.db_table} (event_id, type, payload, received_at, result) "
            "VALUES (%s, %s, %s::jsonb, now(), '') ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
            [event_id, event_type, _json(payload)],
        )
        return cur.fetchone() is not None


def _json(payload):
    import json

    return json.dumps(payload)


def _finish_event(event_id, result):
    StripeEvent.objects.filter(pk=event_id).update(processed_at=timezone.now(), result=result[:200])


def record_stripe_payment(event_id, event_type, payload, invoice_id, amount_cents, fee_cents, payment_intent_id, received_on):
    """Called only after the webhook signature is verified. Duplicate deliveries do nothing."""
    actor = ActorRef.stripe()
    require(actor, "record_online_payment", "document", invoice_id)
    with transaction.atomic():
        if not _claim_event(event_id, event_type, payload):
            return None
        if Payment.objects.filter(stripe_payment_intent_id=payment_intent_id).exists():
            _finish_event(event_id, "duplicate payment intent; ignored")
            return None
        inv = Document.objects.select_for_update().filter(pk=invoice_id).first()
        if inv is None or inv.kind != D.Kind.INVOICE or inv.status not in OPEN_INVOICE:
            # Money arrived for something that isn't open. Keep the event, flag it, change nothing.
            _finish_event(event_id, "NEEDS REVIEW: invoice not open")
            audit.record(actor, "payment_needs_review", "stripe_event", event_id, after={"invoice_id": invoice_id, "amount_cents": amount_cents})
            return None
        p = _apply_payment(
            inv,
            actor,
            Payment.Method.STRIPE,
            amount_cents,
            received_on,
            stripe_fee_cents=fee_cents,
            stripe_payment_intent_id=payment_intent_id,
            stripe_event_id=event_id,
        )
        _finish_event(event_id, f"payment {p.pk}")
        return p


def record_stripe_refund(event_id, event_type, payload, payment_intent_id, refund_id, amount_cents):
    actor = ActorRef.stripe()
    require(actor, "record_online_payment", "payment", payment_intent_id)
    with transaction.atomic():
        if not _claim_event(event_id, event_type, payload):
            return None
        if Refund.objects.filter(stripe_refund_id=refund_id).exists():
            _finish_event(event_id, "duplicate refund; ignored")
            return None
        p = Payment.objects.select_for_update().filter(stripe_payment_intent_id=payment_intent_id).first()
        if p is None:
            _finish_event(event_id, "NEEDS REVIEW: refund for unknown payment")
            audit.record(actor, "refund_needs_review", "stripe_event", event_id, after={"payment_intent": payment_intent_id})
            return None
        already = p.refunds.aggregate(s=Sum("amount_cents"))["s"] or 0
        if amount_cents <= 0 or already + amount_cents > p.amount_cents:
            _finish_event(event_id, "NEEDS REVIEW: refund larger than payment")
            audit.record(actor, "refund_needs_review", "stripe_event", event_id, after={"payment_id": p.pk, "amount_cents": amount_cents})
            return None
        r = Refund.objects.create(payment=p, amount_cents=amount_cents, stripe_refund_id=refund_id, recorded_by=actor.kind)
        audit.record(
            actor,
            "refund_recorded",
            "document",
            p.invoice_id,
            after={"refund_id": r.pk, "amount_cents": amount_cents, "resolution": r.resolution},
            note="waiting for Blake: credit note or reopen balance",
        )
        _finish_event(event_id, f"refund {r.pk}")
        return r


def resolve_refund(actor: ActorRef, refund, resolution, now=None):
    """Blake decides what a refund means: a credit note (nothing owed) or reopening the balance."""
    require(actor, "resolve_refund", "refund", refund.pk)
    if resolution not in (Refund.Resolution.CREDIT_NOTE, Refund.Resolution.REOPEN):
        raise RuleError("choose credit_note or reopen")
    with transaction.atomic():
        r = Refund.objects.select_for_update().get(pk=refund.pk)
        if r.resolution != Refund.Resolution.PENDING:
            raise RuleError("this refund has already been decided")
        inv = Document.objects.select_for_update().get(pk=r.payment.invoice_id)
        r.resolution, r.resolved_at = resolution, timezone.now()
        if resolution == Refund.Resolution.CREDIT_NOTE:
            r.save()
            cn = create_credit_note_draft(
                actor,
                inv,
                [{"description": f"Refund of online payment ({money.format_usd(r.amount_cents)})", "unit_cents": r.amount_cents, "taxable": False}],
            )
            cn = approve(actor, cn, now=now)
            Refund.objects.filter(pk=r.pk).update(credit_note=cn)
        else:
            r.save()
            _refresh_invoice_status(inv, actor=actor, note="refund: balance reopened")
        audit.record(actor, "refund_resolved", "document", inv.pk, after={"refund_id": r.pk, "resolution": resolution, "balance_cents": balance_cents(inv)})
    return r


# --- late fees ----------------------------------------------------------------


def draft_late_fee(actor: ActorRef, invoice, on_date=None):
    """Draft a separate, untaxed late-fee invoice linked to an overdue one. Never sends anything.
    Returns None when no fee applies (late fees off, not 30 days past due, nothing owed, or already drafted)."""
    require(actor, "draft_late_fee", "document", invoice.pk)
    day = on_date or today()
    inv = Document.objects.get(pk=invoice.pk)
    if not inv.late_fee_enabled or inv.kind != D.Kind.INVOICE or inv.status not in (S.SENT, S.PARTLY_PAID) or not inv.due_date:
        return None
    days_late = (day - inv.due_date).days
    period = days_late // 30
    bal = balance_cents(inv)
    if period < 1 or bal <= 0:
        return None
    rate = inv.late_fee_bp if inv.late_fee_bp is not None else BusinessSettings.load().late_fee_default_bp
    fee = money.late_fee_cents(bal, rate, 30)  # one period's fee on today's unpaid balance; never compounding
    if Document.objects.filter(late_fee_for=inv, late_fee_period=period).exclude(status=S.VOID).exists():
        return None
    with transaction.atomic():
        doc = Document.objects.create(
            kind=D.Kind.INVOICE,
            customer=inv.customer,
            prepared_by=actor.kind,
            late_fee_for=inv,
            late_fee_period=period,
            notes=f"Late fee on {inv.number}: {rate / 100:g}% of {money.format_usd(bal)} unpaid, {days_late} days past due.",
        )
        LineItem.objects.create(
            document=doc,
            description=f"Late fee — {inv.number} ({days_late} days past due)",
            unit_cents=fee,
            taxable=False,  # separately stated finance charge (spec §4)
            line_cents=fee,
        )
        _log(actor, "draft_late_fee", doc, after=document_state(doc), note=f"for {inv.number}")
    return doc


# --- recurring ----------------------------------------------------------------


def _advance(day, interval):
    if interval == RecurringTemplate.Interval.MONTHLY:
        y, m = (day.year + (day.month // 12), day.month % 12 + 1)
    else:
        y, m = day.year + 1, day.month
    import calendar

    return dt.date(y, m, min(day.day, calendar.monthrange(y, m)[1]))


def run_recurring(actor: ActorRef, on_date=None):
    """Create renewal drafts that are due within their lead time. Safe to run any number of times."""
    require(actor, "create_recurring_draft", "recurring")
    day = on_date or today()
    created = []
    for tpl in RecurringTemplate.objects.filter(active=True).order_by("pk"):
        with transaction.atomic():
            t = RecurringTemplate.objects.select_for_update().get(pk=tpl.pk)
            if t.next_date - dt.timedelta(days=t.lead_days) > day:
                continue
            if RecurringRun.objects.filter(template=t, period_start=t.next_date).exists():
                continue
            doc = Document.objects.create(kind=D.Kind.INVOICE, customer=t.customer, prepared_by=actor.kind, notes=f"Renewal for {t.next_date:%B %Y}")
            for i, line in enumerate(t.lines):
                LineItem.objects.create(
                    document=doc,
                    position=i,
                    product_id=line.get("product_id"),
                    description=line["description"],
                    qty_milli=line.get("qty_milli", 1000),
                    unit_cents=line["unit_cents"],
                    taxable=bool(line.get("taxable", False)),
                    line_cents=line["unit_cents"],
                )
            RecurringRun.objects.create(template=t, period_start=t.next_date, draft=doc)
            _log(actor, "create_recurring_draft", doc, after=document_state(doc), note=f"renewal {t.next_date}")
            t.next_date = _advance(t.next_date, t.interval)
            t.save(update_fields=["next_date"])
            created.append(doc)
    return created


# --- Ledger's API token -------------------------------------------------------


def _hash_token(raw):
    return hashlib.sha256(raw.encode()).hexdigest()


def create_token(actor: ActorRef, name="Ledger", scopes=("read", "drafts:write"), expires_at=None):
    """Returns (token_row, raw_token). The raw token is shown once and never stored."""
    require(actor, "manage_tokens", "api_token")
    bad = set(scopes) - {"read", "drafts:write"}
    if bad:
        raise RuleError(f"unknown scopes: {sorted(bad)}")
    raw = "ccb_" + secrets.token_urlsafe(32)
    with transaction.atomic():
        t = ApiToken.objects.create(name=name, token_hash=_hash_token(raw), prefix=raw[:12], scopes=list(scopes), expires_at=expires_at)
        audit.record(actor, "create_token", "api_token", t.pk, after={"name": name, "prefix": t.prefix, "scopes": list(scopes)})
    return t, raw


def revoke_token(actor: ActorRef, token):
    require(actor, "manage_tokens", "api_token", token.pk)
    with transaction.atomic():
        ApiToken.objects.filter(pk=token.pk, revoked_at__isnull=True).update(revoked_at=timezone.now())
        audit.record(actor, "revoke_token", "api_token", token.pk, note=token.prefix)


def authenticate_token(raw, now=None):
    """Return the ActorRef for a valid token, else None. Constant-time via hash lookup."""
    if not raw or not raw.startswith("ccb_"):
        return None
    now = now or timezone.now()
    t = ApiToken.objects.filter(token_hash=_hash_token(raw), revoked_at__isnull=True).first()
    if t is None or (t.expires_at and t.expires_at <= now):
        return None
    ApiToken.objects.filter(pk=t.pk).update(last_used_at=now)
    return ActorRef(t.actor, t.prefix, frozenset(t.scopes))
