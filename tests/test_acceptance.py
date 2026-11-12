"""Ledger's acceptance tests (billing spec v0.3 §13, with Blake's 2026-11-12 answers:
$1 pay-page minimum). Fake data only. Numbers in the test names match the spec table."""

import datetime as dt

import pytest
from django.db import DatabaseError, transaction

from billing import audit, services as svc
from billing.models import AuditEvent, CustomerCredit, Document, NumberCounter, Payment, RecurringTemplate, Refund
from billing.permissions import ActorRef, LockedError, PermissionDenied, RuleError

from .conftest import central, line

S = Document.Status


def stripe_pay(inv, cents, event="evt_1", pi="pi_1", fee=0, day=dt.date(2026, 12, 12)):
    return svc.record_stripe_payment(event, "checkout.session.completed", {"sample": True}, inv.pk, cents, fee, pi, day)


# 1 ---------------------------------------------------------------------------
def test_01_quote_accept_convert(blake, ledger, customer, products):
    q = svc.create_draft(
        blake,
        "quote",
        customer,
        [line(products["build"], 75000), line(products["hosting"], 6000), line(products["domain"], 1500)],
    )
    q = svc.approve(blake, q, now=central(2026, 12, 3))
    assert q.number == "Q-2026-0001" and q.due_date is None
    svc.mark_sent(blake, q, "manual", now=central(2026, 12, 3))
    svc.set_quote_outcome(blake, q, "accepted")
    inv = svc.convert_quote(ledger, q)  # Ledger may convert an accepted quote
    q.refresh_from_db()
    assert q.status == S.CONVERTED
    assert inv.status == S.DRAFT and inv.from_quote_id == q.pk and inv.number == ""
    t, ctx = svc.preview_totals(inv, dt.date(2026, 12, 4))
    assert not ctx.missing and ctx.label == "Sales tax (7%, Iowa + Sioux Center)"
    assert (t.tax_cents, t.total_cents) == (5250, 87750)
    inv = svc.approve(blake, inv, now=central(2026, 12, 4))
    assert (inv.number, inv.tax_cents, inv.total_cents) == ("CC-2026-0001", 5250, 87750)
    assert inv.due_date == dt.date(2027, 1, 3)  # Net 30 from the Central-time approval date


# 2 ---------------------------------------------------------------------------
def test_02_tax_per_line_not_on_the_total(blake, customer, products, make_invoice):
    inv = make_invoice([line(products["build"], 3333) for _ in range(3)])
    assert inv.tax_cents == 699  # 3 × $2.33, not 7% of $99.99 ($7.00)
    assert [li.tax_cents for li in inv.lines.all()] == [233, 233, 233]


# 3 ---------------------------------------------------------------------------
def test_03_tax_exempt_customer(make_invoice, exempt_customer, products):
    inv = make_invoice([line(products["build"], 65000)], cust=exempt_customer)
    assert (inv.tax_cents, inv.total_cents) == (0, 65000)
    assert inv.customer_snapshot["tax_exempt"] is True


# 4 ---------------------------------------------------------------------------
def test_04_partial_online_then_check(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    assert inv.total_cents == 10700
    stripe_pay(inv, 4000)
    inv.refresh_from_db()
    assert inv.status == S.PARTLY_PAID and svc.balance_cents(inv) == 6700
    late = inv.due_date + dt.timedelta(days=1)
    assert svc.badges(inv, late) == ["Partly paid", "Overdue"]
    svc.record_manual_payment(blake, inv, "check", 6700, dt.date(2027, 1, 20), check_number="1042")
    inv.refresh_from_db()
    assert inv.status == S.PAID and svc.balance_cents(inv) == 0
    assert svc.badges(inv, late) == ["Paid"]


# 5 ---------------------------------------------------------------------------
def test_05_pay_page_limits(make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    stripe_pay(inv, 4000)  # balance now $67.00
    for bad in (0, -500, 99, 6701):
        with pytest.raises(RuleError):
            svc.check_online_amount(inv, bad)
    for good in (100, 6700):
        svc.check_online_amount(inv, good)
    with pytest.raises(RuleError):
        svc.check_online_amount(inv, 50.0)  # floats never accepted


def test_05b_small_balances(make_invoice, products):
    inv = make_invoice([line(products["hosting"], 1000)])
    stripe_pay(inv, 925)  # $0.75 left, below the $1 minimum: only the whole balance
    svc.check_online_amount(inv, 75)
    with pytest.raises(RuleError):
        svc.check_online_amount(inv, 74)
    svc.record_manual_payment(ActorRef.blake(), inv, "cash", 35, dt.date(2026, 12, 13))  # $0.40 left
    inv.refresh_from_db()
    assert svc.balance_cents(inv) == 40 and not svc.can_pay_online(inv)  # Stripe can't charge under $0.50


# 6 ---------------------------------------------------------------------------
def test_06_duplicate_stripe_notification(make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    assert stripe_pay(inv, 4000, event="evt_dup") is not None
    assert stripe_pay(inv, 4000, event="evt_dup") is None  # same event again
    assert stripe_pay(inv, 4000, event="evt_other", pi="pi_1") is None  # another event, same payment
    assert Payment.objects.filter(invoice=inv).count() == 1


# 7 ---------------------------------------------------------------------------
def test_07_void_and_reissue_no_gap(blake, make_invoice, products):
    first = make_invoice([line(products["build"], 50000)])
    assert first.number == "CC-2026-0001"
    svc.void(blake, first, "wrong amount (sample)")
    new = svc.create_replacement(blake, Document.objects.get(pk=first.pk))
    new = svc.approve(blake, new, now=central(2026, 12, 11))
    first.refresh_from_db()
    assert first.status == S.VOID
    assert new.number == "CC-2026-0002" and new.replaces_id == first.pk


def test_07b_cannot_void_with_payments(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    svc.record_manual_payment(blake, inv, "cash", 1000, dt.date(2026, 12, 11))
    with pytest.raises(RuleError):
        svc.void(blake, inv, "sample")


# 8 ---------------------------------------------------------------------------
def test_08_discarded_draft_and_failed_approval_use_no_number(blake, customer, products):
    d1 = svc.create_draft(blake, "invoice", customer, [line(products["build"], 1000)])
    svc.delete_draft(blake, d1)

    d2 = svc.create_draft(blake, "invoice", customer, [line(products["build"], 1000)])

    def broken_pdf(doc):
        raise RuntimeError("PDF renderer crashed (simulated)")

    with pytest.raises(RuntimeError):
        svc.approve(blake, d2, now=central(2026, 12, 10), render_pdf=broken_pdf)
    d2.refresh_from_db()
    assert d2.status == S.DRAFT and d2.number == ""
    assert not NumberCounter.objects.filter(series="CC", year=2026, next_seq__gt=1).exists()

    d2 = svc.approve(blake, d2, now=central(2026, 12, 10), render_pdf=lambda doc: (f"pdf/{doc.number}.pdf", "0" * 64))
    assert d2.number == "CC-2026-0001" and d2.pdf_file == "pdf/CC-2026-0001.pdf"


# 9 ---------------------------------------------------------------------------
def test_09_year_rollover_in_central_time(blake, customer, products):
    a = svc.create_draft(blake, "invoice", customer, [line(products["build"], 1000)])
    b = svc.create_draft(blake, "invoice", customer, [line(products["build"], 1000)])
    a = svc.approve(blake, a, now=central(2026, 12, 31, 23, 59, 59))
    b = svc.approve(blake, b, now=central(2027, 1, 1, 0, 0, 0))
    assert a.number == "CC-2026-0001" and a.issue_date == dt.date(2026, 12, 31)
    assert b.number == "CC-2027-0001" and b.issue_date == dt.date(2027, 1, 1)
    # 11:30 PM Dec 31 Central is already Jan 1 in UTC; the Central date must win.
    c = svc.create_draft(blake, "invoice", customer, [line(products["build"], 1000)])
    c = svc.approve(blake, c, now=central(2026, 12, 31, 23, 30))
    assert c.number == "CC-2026-0002"


# 10 --------------------------------------------------------------------------
def test_10_approved_invoice_cannot_be_edited(blake, ledger, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    for who in (blake, ledger):
        with pytest.raises(LockedError):
            svc.update_draft(who, inv, notes="changed")
    assert AuditEvent.objects.filter(entity="document", entity_id=str(inv.pk), action="denied:edit").count() == 2
    # Even direct database writes are refused.
    with pytest.raises(DatabaseError), transaction.atomic():
        Document.objects.filter(pk=inv.pk).update(total_cents=1)
    with pytest.raises(DatabaseError), transaction.atomic():
        inv.lines.update(unit_cents=1)
    with pytest.raises(DatabaseError), transaction.atomic():
        Document.objects.filter(pk=inv.pk).delete()


# 11 --------------------------------------------------------------------------
def test_11_ledger_is_refused_and_logged(blake, ledger, customer, make_invoice, products):
    draft = svc.create_draft(ledger, "invoice", customer, [line(products["build"], 10000)])
    assert "Prepared by Ledger — needs review" in svc.badges(draft)
    inv = make_invoice([line(products["build"], 10000)], sent=False)
    attempts = [
        ("approve", lambda: svc.approve(ledger, draft)),
        ("send", lambda: svc.mark_sent(ledger, inv, "manual")),
        ("send", lambda: svc.mark_sent(ledger, inv, "email", ["x@example.com"])),
        ("void", lambda: svc.void(ledger, inv, "sample")),
        ("record_manual_payment", lambda: svc.record_manual_payment(ledger, inv, "check", 100, dt.date(2026, 12, 11))),
        ("delete_draft", lambda: svc.delete_draft(ledger, draft)),
        ("edit_customer_tax", lambda: svc.update_customer(ledger, customer, tax_exempt=True)),
        ("edit_billing_terms", lambda: svc.update_draft(ledger, draft, late_fee_enabled=True)),
        ("credit_note", lambda: svc.create_credit_note_draft(ledger, inv, [line(cents=100, taxable=False)])),
        ("manage_tokens", lambda: svc.create_token(ledger)),
        ("edit_settings", lambda: svc.update_settings(ledger, min_online_payment_cents=500)),
    ]
    for capability, attempt in attempts:
        with pytest.raises(PermissionDenied):
            attempt()
        assert AuditEvent.objects.filter(actor="ledger", action=f"denied:{capability}").exists(), capability
    # Ledger can't flip a product's taxable default either.
    with pytest.raises(PermissionDenied):
        svc.create_draft(ledger, "invoice", customer, [line(products["hosting"], 6000, taxable=True)])
    assert AuditEvent.objects.filter(actor="ledger", action="denied:override_taxable").exists()
    inv.refresh_from_db()
    assert inv.status == S.APPROVED and not inv.payments.exists()


def test_11b_token_scopes(db, blake):
    tok, raw = svc.create_token(blake)
    who = svc.authenticate_token(raw)
    assert who.kind == "ledger" and who.scopes == frozenset({"read", "drafts:write"})
    assert svc.authenticate_token(raw + "x") is None
    svc.revoke_token(blake, tok)
    assert svc.authenticate_token(raw) is None
    _, read_only = svc.create_token(blake, scopes=("read",))
    ro = svc.authenticate_token(read_only)
    from billing.permissions import allowed

    assert allowed(ro, "view") and not allowed(ro, "edit_draft")


# 12 --------------------------------------------------------------------------
def test_12_late_fee_draft(blake, ledger, make_invoice, products):
    inv = make_invoice([line(products["hosting"], 50000)], late_fee_enabled=True)
    assert inv.total_cents == 50000
    day = inv.due_date + dt.timedelta(days=31)
    fee = svc.draft_late_fee(ledger, inv, day)
    assert fee.status == S.DRAFT and fee.late_fee_for_id == inv.pk
    assert [(li.unit_cents, li.taxable) for li in fee.lines.all()] == [(1000, False)]
    assert svc.draft_late_fee(ledger, inv, day) is None  # once per period
    assert svc.draft_late_fee(ledger, inv, inv.due_date + dt.timedelta(days=20)) is None
    fee = svc.approve(blake, fee, now=central(2027, 2, 12))
    assert fee.tax_cents == 0 and fee.total_cents == 1000 and fee.sent_at is None  # nothing sent

    off = make_invoice([line(products["hosting"], 50000)])
    assert svc.draft_late_fee(ledger, off, off.due_date + dt.timedelta(days=31)) is None


# 13 --------------------------------------------------------------------------
def test_13_scheduler_twice_one_draft(customer, products):
    from billing.permissions import ActorRef

    RecurringTemplate.objects.create(
        customer=customer,
        next_date=dt.date(2027, 3, 15),
        lines=[
            {"description": "Web hosting (annual)", "unit_cents": 6000, "taxable": False},
            {"description": "Domain registration (annual)", "unit_cents": 1295, "taxable": False},
        ],
    )
    sched = ActorRef.scheduler()
    assert svc.run_recurring(sched, dt.date(2027, 2, 28)) == []  # 15 days early: not yet
    first = svc.run_recurring(sched, dt.date(2027, 3, 1))
    second = svc.run_recurring(sched, dt.date(2027, 3, 1))
    assert len(first) == 1 and second == []
    draft = first[0]
    assert draft.status == S.DRAFT and draft.prepared_by == "scheduler"
    assert sum(li.unit_cents for li in draft.lines.all()) == 7295


# 14 --------------------------------------------------------------------------
def test_14_overpaid_check_becomes_credit(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    p = svc.record_manual_payment(blake, inv, "check", 11000, dt.date(2026, 12, 15), check_number="2001")
    inv.refresh_from_db()
    assert inv.status == S.PAID and p.applied_cents == 10700
    assert CustomerCredit.objects.get(source_payment=p).amount_cents == 300


# 15 --------------------------------------------------------------------------
def test_15_donated_invoice(blake, customer, products):
    d = svc.create_draft(blake, "invoice", customer, [line(products["build"], 65000)], donated=True)
    d = svc.approve(blake, d, now=central(2026, 12, 10))
    assert (d.value_cents, d.total_cents, d.tax_cents) == (65000, 0, 0)
    assert "Donated" in svc.badges(d)
    svc.mark_sent(blake, d, "manual", now=central(2026, 12, 10))
    d.refresh_from_db()
    assert d.status == S.PAID and not svc.can_pay_online(d)
    from billing.models import Receipt

    assert not Receipt.objects.filter(payment__invoice=d).exists()  # no payment, no receipt


# 16 --------------------------------------------------------------------------
def test_16_mark_sent_manually(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)], sent=False)
    warnings = svc.mark_sent(blake, inv, "manual", now=central(2026, 12, 20))
    inv.refresh_from_db()
    assert inv.status == S.SENT and inv.sent_method == "manual"
    assert warnings and "10 days" in warnings[0]  # approved Dec 10, sent Dec 20 (> 7 days)
    ev = AuditEvent.objects.filter(entity="document", entity_id=str(inv.pk), action="sent_manually").get()
    assert ev.note == "sent manually" and ev.actor == "blake"


# 17 --------------------------------------------------------------------------
@pytest.mark.skip(reason="Email sending is built at step 5 (SMTP via ArkHost, set up at the test install)")
def test_17_email_from_blake():
    pass


# 18 --------------------------------------------------------------------------
@pytest.mark.parametrize("choice", ["credit_note", "reopen"])
def test_18_refund_needs_blakes_decision(blake, make_invoice, products, choice):
    inv = make_invoice([line(products["build"], 10000)])
    stripe_pay(inv, 10700, event="evt_pay", pi="pi_full")
    r = svc.record_stripe_refund("evt_ref", "charge.refunded", {"sample": True}, "pi_full", "re_1", 4000)
    assert svc.record_stripe_refund("evt_ref", "charge.refunded", {}, "pi_full", "re_1", 4000) is None  # duplicate
    inv.refresh_from_db()
    assert r.resolution == Refund.Resolution.PENDING
    assert inv.status == S.PAID and svc.balance_cents(inv) == 0  # nothing decided silently
    svc.resolve_refund(blake, r, choice, now=central(2026, 12, 20))
    inv.refresh_from_db()
    if choice == "credit_note":
        cn = Document.objects.get(kind="credit_note", credit_for=inv)
        assert cn.number == "CN-2026-0001" and cn.total_cents == 4000
        assert svc.balance_cents(inv) == 0 and inv.status == S.PAID
    else:
        assert svc.balance_cents(inv) == 4000 and inv.status == S.PARTLY_PAID
    with pytest.raises(RuleError):
        svc.resolve_refund(blake, Refund.objects.get(pk=r.pk), "reopen")


# 19 --------------------------------------------------------------------------
@pytest.mark.skip(reason="Stripe history import moves to January (tech proposal v0.2 §6)")
def test_19_stripe_import():
    pass


# 20 --------------------------------------------------------------------------
def test_20_every_step_is_in_the_history(blake, ledger, customer, products):
    d = svc.create_draft(ledger, "invoice", customer, [line(products["build"], 10000)])
    svc.update_draft(ledger, d, notes="Thanks! (sample)")
    d = svc.approve(blake, d, now=central(2026, 12, 10))
    svc.mark_sent(blake, d, "manual", now=central(2026, 12, 10))
    stripe_pay(d, 4000)
    svc.record_manual_payment(blake, d, "check", 6700, dt.date(2026, 12, 20), check_number="1042")

    steps = list(audit.history("document", d.pk).values_list("actor", "action"))
    assert steps == [
        ("ledger", "create_draft"),
        ("ledger", "update_draft"),
        ("blake", "approve"),
        ("blake", "sent_manually"),
        ("stripe", "payment"),
        ("blake", "payment"),
        ("system", "paid_copy_created"),
    ]
    edit = audit.history("document", d.pk).get(action="update_draft")
    assert edit.before["notes"] == "" and edit.after["notes"] == "Thanks! (sample)"
    approval = audit.history("document", d.pk).get(action="approve")
    assert approval.before["number"] == "" and approval.after["number"] == "CC-2026-0001"
    assert all(e.at is not None for e in audit.history("document", d.pk))
    assert audit.verify_chain()[0]


def test_untaxed_defaults_hosting_domains_platform(db):
    """Blake confirmed: hosting, domains and platform renewals (e.g. Webflow) are not taxable."""
    from billing.models import Product

    for kind in ("hosting", "domain", "platform", "late_fee"):
        assert not Product.objects.filter(kind=kind, default_taxable=True).exists(), kind
