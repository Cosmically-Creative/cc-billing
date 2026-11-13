"""PDFs for quotes, invoices and credit notes (WeasyPrint).

`document_context()` turns a document into ready-to-print strings: every amount, date and label is
formatted here, from the frozen values stored at approval. The template only lays text out; it does
no arithmetic. The layout follows docs/pdf-design.md (Prism's Penpot design).

Stored PDFs: the original is rendered once at approval (services.approve's render_pdf hook); the
PAID copy once when the balance reaches $0 (services.deliver_receipt). Both are never re-rendered.
"""

import hashlib
import io
import secrets
from urllib.parse import urlparse, unquote
from pathlib import Path

import qrcode
import qrcode.image.svg
from django.conf import settings
from django.utils import timezone
from django.template.loader import render_to_string

from . import money
from .models import Document, Payment

D = Document
S = Document.Status
STATIC = Path(__file__).resolve().parent / "static" / "billing"

KIND_LABEL = {D.Kind.INVOICE: "INVOICE", D.Kind.QUOTE: "QUOTE", D.Kind.CREDIT_NOTE: "CREDIT NOTE"}
NUMBER_LABEL = {D.Kind.INVOICE: "Invoice no.", D.Kind.QUOTE: "Quote no.", D.Kind.CREDIT_NOTE: "Credit note no."}
METHOD = {Payment.Method.STRIPE: "Online", Payment.Method.CHECK: "Check", Payment.Method.CASH: "Cash", Payment.Method.OTHER: "Payment"}


def base_url():
    return getattr(settings, "CCB_BASE_URL", "https://billing.cosmicallycreative.com").rstrip("/")


def fmt_date(day):
    return f"{day:%b} {day.day}, {day.year}" if day else ""


def fmt_qty(qty_milli):
    whole, frac = divmod(qty_milli, 1000)
    return f"{whole:,}" if not frac else f"{whole:,}.{frac:03d}".rstrip("0")


def qr_svg(text):
    img = qrcode.make(text, image_factory=qrcode.image.svg.SvgPathImage, box_size=10, border=4)
    buf = io.BytesIO()
    img.save(buf)
    return buf.getvalue().decode()


def _bill_to(snapshot):
    lines = [snapshot.get("name", "")]
    lines += [ln.strip() for ln in (snapshot.get("address") or "").splitlines() if ln.strip()]
    return [ln for ln in lines if ln]


def document_context(doc, *, stamp=None, paid_copy=None, payments=None, replacement=None, page_note=""):
    """Everything the template prints. `stamp` is None, "paid" or "void".
    `paid_copy` (PaidCopy) provides the frozen payment list for the PAID stamp."""
    biz = doc.business_snapshot or {}
    cust = doc.customer_snapshot or {}
    sign = -1 if doc.kind == D.Kind.CREDIT_NOTE else 1
    usd = lambda c: money.format_usd(sign * c)  # noqa: E731

    lines = []
    running = 0
    for li in doc.lines.all():
        running += li.line_cents
        lines.append(
            {
                "running_subtotal": usd(running),
                "description": li.description,
                "detail": "",
                "qty": fmt_qty(li.qty_milli),
                "rate": money.format_usd(li.unit_cents),
                "taxed": li.tax_cents > 0 or (li.taxable and li.tax_rate_bp > 0),
                "tax_mark": "T" if li.taxable else "–",
                "discount": (
                    {"label": li.discount_label or "Discount", "amount": money.format_usd(-sign * li.discount_cents)}
                    if li.discount_cents
                    else None
                ),
                "amount": usd(money.div_round_half_up(li.unit_cents * li.qty_milli, 1000)),
            }
        )

    meta = [("Issue date", fmt_date(doc.issue_date))]
    if doc.kind == D.Kind.INVOICE:
        meta += [("Due date", fmt_date(doc.due_date)), ("Terms", "Net 30")]
    meta.append((NUMBER_LABEL[doc.kind], doc.number))
    applied_to = None
    if doc.kind == D.Kind.CREDIT_NOTE and doc.credit_for_id:
        inv = Document.objects.get(pk=doc.credit_for_id)
        meta.append(("Applies to", inv.number))
        applied_to = {
            "invoice": inv.number,
            "invoice_total": money.format_usd(inv.total_cents),
            "credit": money.format_usd(-doc.total_cents),
            "remaining": money.format_usd(inv.total_cents - doc.total_cents),
        }

    tax_split = ""
    if doc.local_tax_cents and doc.tax_jurisdiction_id:
        tax_split = (
            f"Iowa {money.format_rate(doc.state_rate_bp or 0)} {usd(doc.state_tax_cents)} · "
            f"{doc.tax_jurisdiction.name} {money.format_rate(doc.local_rate_bp or 0)} {usd(doc.local_tax_cents)}"
        )

    paid_rows, paid_cents = [], 0
    if doc.kind == D.Kind.INVOICE:
        for p in payments if payments is not None else doc.payments.order_by("received_on", "pk"):
            if not p.applied_cents:
                continue
            paid_cents += p.applied_cents
            detail = METHOD.get(p.method, "Payment")
            if p.method == Payment.Method.CHECK and p.check_number:
                detail += f" #{p.check_number}"
            paid_rows.append({"detail": f"{detail}, {fmt_date(p.received_on)}", "amount": money.format_usd(-p.applied_cents)})

    if paid_copy is not None:
        paid_rows = [{"detail": f"{i.get('method', i['type']).replace('_', ' ').title()}, {i['date']}",
                      "amount": money.format_usd(-i['amount_cents'])} for i in paid_copy.applied]
    if doc.kind == D.Kind.INVOICE and doc.pk and paid_copy is None:
        for credit in doc.credit_notes.exclude(status__in=[S.DRAFT, S.VOID]).order_by("pk"):
            paid_rows.append({"detail": f"Credit note {credit.number}", "amount": money.format_usd(-credit.total_cents)})
        from .models import Refund
        for refund in Refund.objects.filter(payment__invoice=doc).exclude(resolution=Refund.Resolution.PENDING).order_by("pk"):
            paid_rows.append({"detail": "Refund", "amount": money.format_usd(refund.amount_cents)})
    # Stored approval PDFs are created before the document row changes from draft.
    from .services import balance_cents
    balance = 0 if stamp else (max(balance_cents(doc), 0) if doc.pk and not doc.is_draft else max(doc.total_cents - paid_cents, 0))
    pay_url = f"{base_url()}/pay/{doc.pay_code}" if doc.pay_code else ""
    show_pay = (
        doc.kind == D.Kind.INVOICE
        and not stamp
        and bool(pay_url)
        and not doc.pay_link_disabled
        and not doc.donated
        and balance >= 50  # Stripe's floor
    )

    stamp_ctx = None
    if stamp == "paid":
        items = (paid_copy.applied if paid_copy else []) or []
        stamp_ctx = {
            "kind": "PAID",
            "date": fmt_date(paid_copy.paid_in_full_on) if paid_copy else "",
            "lines": [f"{money.format_usd(i['amount_cents'])} · {i['date']}" for i in items],
        }
    elif stamp == "void":
        stamp_ctx = {
            "kind": "VOID",
            "date": fmt_date(timezone.localdate(doc.voided_at)) if doc.voided_at else "",
            "lines": [f"Replaced by {replacement.number}"] if replacement and replacement.number else [],
        }

    return {
        "kind": doc.kind,
        "kind_label": KIND_LABEL[doc.kind],
        "number": doc.number,
        "business": {
            "name": biz.get("name", ""),
            "address_line1": biz.get("address_line1", ""),
            "city_state_zip": biz.get("city_state_zip", ""),
            "email": biz.get("email", ""),
            "phone": biz.get("phone", ""),
        },
        "bill_to": _bill_to(cust),
        "meta": [{"label": a, "value": b} for a, b in meta],
        "lines": lines,
        "totals": {
            "subtotal": usd(doc.subtotal_cents),
            "discount": usd(-doc.discount_cents) if doc.discount_cents else "",
            "taxable": usd(doc.taxable_cents),
            "tax_label": doc.tax_label or "Sales tax",
            "tax": usd(doc.tax_cents),
            "tax_split": tax_split,
            "show_tax": bool(doc.tax_cents) or doc.out_of_state,
            "total": usd(doc.total_cents),
            "payments": paid_rows,
            "balance_due": money.format_usd(balance),
            "show_balance": doc.kind == D.Kind.INVOICE,
        },
        "donated": {"value": usd(doc.value_cents)} if doc.donated else None,
        "out_of_state": doc.out_of_state,
        "pay": {"show": show_pay, "url": pay_url, "url_text": pay_url.split("://", 1)[-1], "qr_svg": qr_svg(pay_url) if show_pay else ""},
        "check_text": (
            f"Payable to {biz.get('name', '')}. Mail to {biz.get('address_line1', '')}, {biz.get('city_state_zip', '')}, "
            f"with {doc.number} on the memo line."
            if doc.kind == D.Kind.INVOICE and not stamp and balance > 0
            else ""
        ),
        "notes": doc.notes,
        "late_fee_text": (
            f"A late fee of {money.format_rate(doc.late_fee_bp or 200)} per month applies to balances unpaid after the due date."
            if doc.late_fee_enabled and doc.kind == D.Kind.INVOICE
            else ""
        ),
        "footer_left": " · ".join(x for x in (biz.get("name"), f"{biz.get('address_line1', '')}, {biz.get('city_state_zip', '')}", biz.get("phone")) if x),
        "stamp": stamp_ctx,
        "applied_to": applied_to,
        "page_note": page_note,
        "fonts_dir": (STATIC / "fonts").as_uri(),
        "logo_uri": (STATIC / "cc-logo-v11.svg").as_uri(),
    }


def render_html(ctx):
    return render_to_string("billing/pdf/document.html", ctx)


def asset_fetcher(url):
    """PDF templates can fetch only the bundled logo and fonts; never network URLs."""
    from weasyprint import default_url_fetcher
    parsed = urlparse(url)
    path = Path(unquote(parsed.path)).resolve()
    if parsed.scheme != "file" or parsed.netloc or not path.is_relative_to(STATIC.resolve()):
        raise ValueError("PDF asset is outside bundled assets")
    return default_url_fetcher(url)


def render_pdf_bytes(ctx):
    from weasyprint import HTML, CSS
    html = HTML(string=render_html(ctx), base_url=str(STATIC), url_fetcher=asset_fetcher)
    first = html.render()
    rules = []
    # Read actual laid-out rows, so long/wrapped descriptions and discounts paginate naturally.
    # Only non-final pages get a subtotal; totals/payment instructions stay at the document end.
    for index, page in enumerate(first.pages[:-1], 1):
        subtotal = None
        for box in page._page_box.descendants():
            if type(box).__name__ == "TableRowBox" and box.element is not None:
                subtotal = box.element.get("data-subtotal") or subtotal
        if subtotal:
            rules.append('@page :nth(%d) { @bottom-center { content: "Subtotal so far: %s"; } }' % (index, subtotal))
    return html.write_pdf(stylesheets=[CSS(string="\n".join(rules))]) if rules else first.write_pdf()


def _store(relpath, data):
    # Unique immutable file per render: a failed approval can leave an orphan, but never
    # prevents retrying the same gapless invoice number. No existing file is overwritten.
    rel = Path(relpath)
    relpath = str(rel.with_name(f"{rel.stem}-{secrets.token_hex(8)}{rel.suffix}"))
    path = Path(settings.MEDIA_ROOT) / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"{relpath} already exists; stored PDFs are never overwritten")
    with path.open("xb") as stream:
        stream.write(data)
    return relpath, hashlib.sha256(data).hexdigest()


def render_approved(doc):
    """approve() hook: render and store the original PDF. Returns (relative path, sha256)."""
    return _store(f"pdf/{doc.year}/{doc.number}.pdf", render_pdf_bytes(document_context(doc)))


def render_paid_copy(copy):
    """deliver_receipt() hook: render and store the PAID-stamped copy once."""
    inv = copy.invoice
    ctx = document_context(inv, stamp="paid", paid_copy=copy)
    return _store(f"pdf/{inv.year}/{inv.number}-PAID-{copy.pk}.pdf", render_pdf_bytes(ctx))
