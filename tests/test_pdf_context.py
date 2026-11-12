"""Tests for billing/pdf.py's document_context() (fake data only).

Every amount, date and label the builder produces, checked against the frozen
values on the document — the template only lays text out, it does no arithmetic.
"""

import datetime as dt

from billing import pdf, services as svc

from .conftest import central, line


# 1 ---------------------------------------------------------------------------
def test_01_fmt_qty(db):
    assert pdf.fmt_qty(1000) == "1"
    assert pdf.fmt_qty(1500) == "1.5"
    assert pdf.fmt_qty(2250) == "2.25"
    assert pdf.fmt_qty(12000) == "12"
    assert pdf.fmt_qty(1234567) == "1,234.567"


# 2 ---------------------------------------------------------------------------
def test_02_fmt_date(db):
    assert pdf.fmt_date(dt.date(2026, 12, 3)) == "Dec 3, 2026"
    assert pdf.fmt_date(None) == ""


# 3 ---------------------------------------------------------------------------
def test_03_invoice_context(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000), line(products["hosting"], 6000)])
    inv.refresh_from_db()
    ctx = pdf.document_context(inv)
    assert ctx["kind_label"] == "INVOICE" and ctx["number"] == "CC-2026-0001"
    assert [m["label"] for m in ctx["meta"]] == ["Issue date", "Due date", "Terms", "Invoice no."]
    assert [li["tax_mark"] for li in ctx["lines"]] == ["T", "–"]  # tax on the build line only
    assert [li["amount"] for li in ctx["lines"]] == ["$100.00", "$60.00"]
    t = ctx["totals"]
    assert t["subtotal"] == "$160.00" and t["taxable"] == "$100.00" and t["tax"] == "$7.00"
    assert t["total"] == "$167.00"
    assert t["tax_label"] == "Sales tax (7%, Iowa + Sioux Center)"
    assert t["tax_split"] == "Iowa 6% $6.00 · Sioux Center 1% $1.00"
    assert t["balance_due"] == "$167.00"
    assert ctx["pay"]["show"] is True
    assert ctx["pay"]["url"].endswith("/pay/" + inv.pay_code)
    assert ctx["pay"]["qr_svg"].startswith("<svg") or ctx["pay"]["qr_svg"].startswith("<?xml")
    assert inv.number in ctx["check_text"]


# 4 ---------------------------------------------------------------------------
def test_04_payment_row_in_context(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    svc.record_manual_payment(blake, inv, "check", 6700, dt.date(2026, 12, 20), check_number="1042")
    inv.refresh_from_db()
    ctx = pdf.document_context(inv)
    rows = ctx["totals"]["payments"]
    assert len(rows) == 1
    assert rows[0]["detail"] == "Check #1042, Dec 20, 2026"
    assert rows[0]["amount"] == "-$67.00"
    assert ctx["totals"]["balance_due"] == "$40.00"  # $107.00 - $67.00


# 5 ---------------------------------------------------------------------------
def test_05_paid_invoice_hides_pay_and_check_text(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    svc.record_manual_payment(blake, inv, "check", 6700, dt.date(2026, 12, 20), check_number="1042")
    svc.record_manual_payment(blake, inv, "check", 10000, dt.date(2026, 12, 22), check_number="1043")
    inv.refresh_from_db()
    ctx = pdf.document_context(inv)
    assert ctx["pay"]["show"] is False
    assert ctx["check_text"] == ""


# 6 ---------------------------------------------------------------------------
def test_06_exempt_customer_no_tax(make_invoice, exempt_customer, products):
    inv = make_invoice([line(products["build"], 65000)], cust=exempt_customer)
    inv.refresh_from_db()
    t = pdf.document_context(inv)["totals"]
    assert t["show_tax"] is False
    assert t["tax"] == "$0.00"
    assert t["total"] == "$650.00"


# 7 ---------------------------------------------------------------------------
def test_07_quote_context(blake, customer, products):
    q = svc.create_draft(blake, "quote", customer, [line(products["build"], 75000)])
    q = svc.approve(blake, q, now=central(2026, 12, 3))
    q.refresh_from_db()
    ctx = pdf.document_context(q)
    assert ctx["kind_label"] == "QUOTE"
    assert [m["label"] for m in ctx["meta"]] == ["Issue date", "Quote no."]
    assert ctx["pay"]["show"] is False
    assert ctx["totals"]["show_balance"] is False


# 8 ---------------------------------------------------------------------------
def test_08_late_fee_text(make_invoice, products):
    on = make_invoice([line(products["build"], 10000)], late_fee_enabled=True)
    on.refresh_from_db()
    assert (
        pdf.document_context(on)["late_fee_text"]
        == "A late fee of 2% per month applies to balances unpaid after the due date."
    )
    off = make_invoice([line(products["build"], 10000)])
    off.refresh_from_db()
    assert pdf.document_context(off)["late_fee_text"] == ""


# 9 ---------------------------------------------------------------------------
def test_09_donated_invoice(blake, customer, products):
    d = svc.create_draft(blake, "invoice", customer, [line(products["build"], 65000)], donated=True)
    d = svc.approve(blake, d, now=central(2026, 12, 10))
    d.refresh_from_db()
    ctx = pdf.document_context(d)
    assert ctx["donated"] == {"value": "$650.00"}
    assert ctx["totals"]["total"] == "$0.00"
    assert ctx["pay"]["show"] is False


# 10 --------------------------------------------------------------------------
def test_10_void_stamp(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    svc.void(blake, inv, "sample")
    inv.refresh_from_db()
    ctx = pdf.document_context(inv, stamp="void")
    assert ctx["stamp"]["kind"] == "VOID"
    assert ctx["totals"]["balance_due"] == "$0.00"
