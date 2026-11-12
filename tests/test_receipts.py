"""Receipts and the PAID-stamped copy (billing spec v0.6 §6, tests 21–25). Fake data only.

The PDF renderer and the mail sender are stand-ins here; the real ones (WeasyPrint, SMTP via
ArkHost) plug into the same two callables."""

import datetime as dt

import pytest
from django.db import DatabaseError, transaction

from billing import services as svc
from billing.models import AuditEvent, BusinessSettings, Document, PaidCopy, Receipt
from billing.permissions import PermissionDenied

from .conftest import central, line

S = Document.Status
R = Receipt.EmailStatus


class FakeMail:
    def __init__(self, fail=False):
        self.sent, self.fail = [], fail

    def __call__(self, receipt, summary, paid_copy):
        if self.fail:
            raise ConnectionError("mail server down (simulated)")
        self.sent.append((summary, paid_copy.pdf_file if paid_copy else None))


class FakeRenderer:
    def __init__(self):
        self.calls = 0

    def __call__(self, copy):
        self.calls += 1
        return f"paid/{copy.invoice.number}-{copy.pk}.pdf", "a" * 64


def stripe_pay(inv, cents, event, pi):
    return svc.record_stripe_payment(event, "checkout.session.completed", {"sample": True}, inv.pk, cents, 0, pi, dt.date(2026, 12, 12))


def test_21_online_partial_receipt_once(make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    stripe_pay(inv, 4000, "evt_a", "pi_a")
    stripe_pay(inv, 4000, "evt_a", "pi_a")  # delivered twice
    assert Receipt.objects.filter(payment__invoice=inv).count() == 1
    mail, render = FakeMail(), FakeRenderer()
    r = Receipt.objects.get(payment__invoice=inv)
    svc.deliver_receipt(r, render, mail)
    svc.deliver_receipt(r, render, mail)  # a second delivery attempt sends nothing
    assert len(mail.sent) == 1
    summary, attachment = mail.sent[0]
    assert summary["from"] == "blake@cosmicallycreative.com" and summary["to"] == ["owner@example.com"]
    assert (summary["invoice_number"], summary["amount"], summary["method"]) == (inv.number, "$40.00", "Card/bank (online)")
    assert (summary["paid_so_far"], summary["balance"]) == ("$40.00", "$67.00")
    assert attachment is None and render.calls == 0  # partial payments attach no PDF
    assert AuditEvent.objects.filter(action="receipt_sent", entity_id=str(inv.pk)).count() == 1


def test_22_check_paid_in_full_attaches_paid_copy_and_cash_unticked(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    stripe_pay(inv, 4000, "evt_a", "pi_a")
    p = svc.record_manual_payment(blake, inv, "check", 6700, dt.date(2026, 12, 20), check_number="1042")
    mail, render = FakeMail(), FakeRenderer()
    svc.deliver_receipt(p.receipt, render, mail)
    summary, attachment = mail.sent[0]
    assert summary["balance"] == "Paid in full" and summary["method"] == "Check #1042"
    assert attachment and attachment.startswith("paid/")

    other = make_invoice([line(products["hosting"], 2000)])
    cash = svc.record_manual_payment(blake, other, "cash", 2000, dt.date(2026, 12, 20), email_receipt=False)
    assert cash.receipt.email_status == R.NOT_SENT
    svc.deliver_receipt(cash.receipt, render, mail)
    assert len(mail.sent) == 1  # nothing emailed for the unticked one
    assert AuditEvent.objects.filter(action="receipt_not_sent", note="receipt not sent", entity_id=str(other.pk)).exists()


def test_23_mail_failure_keeps_payment_and_resend_uses_same_copy(blake, ledger, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    p = svc.record_manual_payment(blake, inv, "check", 10700, dt.date(2026, 12, 20), check_number="7")
    render = FakeRenderer()
    r = svc.deliver_receipt(p.receipt, render, FakeMail(fail=True))
    inv.refresh_from_db()
    assert r.email_status == R.FAILED and "mail server down" in r.last_error
    assert inv.status == S.PAID and inv.payments.count() == 1  # payment untouched
    assert AuditEvent.objects.filter(action="receipt_failed").exists()

    with pytest.raises(PermissionDenied):
        svc.resend_receipt(ledger, r, render, FakeMail())
    mail = FakeMail()
    r = svc.resend_receipt(blake, Receipt.objects.get(pk=r.pk), render, mail)
    assert r.email_status == R.SENT and len(mail.sent) == 1
    assert render.calls == 1  # the stored PAID copy was made once and reused
    assert mail.sent[0][1] == PaidCopy.objects.get(invoice=inv).pdf_file


def test_24_pay_minimum_setting(blake, ledger, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    svc.check_online_amount(inv, 100)
    svc.update_settings(blake, min_online_payment_cents=500)
    with pytest.raises(svc.RuleError):
        svc.check_online_amount(inv, 499)
    svc.check_online_amount(inv, 500)
    assert AuditEvent.objects.filter(action="update_settings", actor="blake").exists()
    with pytest.raises(PermissionDenied):
        svc.update_settings(ledger, min_online_payment_cents=100)
    assert BusinessSettings.load().min_online_payment_cents == 500
    with pytest.raises(svc.RuleError):
        svc.update_settings(blake, min_online_payment_cents=10)  # below Stripe's $0.50


def test_25_paid_copy_is_separate_and_superseded_after_reopen(blake, customer, products):
    d = svc.create_draft(blake, "invoice", customer, [line(products["build"], 10000)])
    d = svc.approve(blake, d, now=central(2026, 12, 10), render_pdf=lambda doc: (f"pdf/{doc.number}.pdf", "b" * 64))
    svc.mark_sent(blake, d, "manual", now=central(2026, 12, 10))
    original = (d.pdf_file, d.pdf_sha256)

    stripe_pay(d, 4000, "evt_a", "pi_a")
    svc.record_manual_payment(blake, d, "check", 6700, dt.date(2026, 12, 20), check_number="1042")
    copy = PaidCopy.objects.get(invoice=d)
    assert [i["amount_cents"] for i in copy.applied] == [4000, 6700]
    assert [i["date"] for i in copy.applied] == ["2026-12-12", "2026-12-20"]
    d.refresh_from_db()
    assert (d.pdf_file, d.pdf_sha256) == original  # the approval PDF never changes

    refund = svc.record_stripe_refund("evt_r", "charge.refunded", {"sample": True}, "pi_a", "re_1", 4000)
    svc.resolve_refund(blake, refund, "reopen")
    d.refresh_from_db()
    copy.refresh_from_db()
    assert copy.superseded_at is not None  # kept, marked superseded
    assert svc.balance_cents(d) == 4000 and d.status == S.PARTLY_PAID
    assert not PaidCopy.objects.filter(invoice=d, superseded_at__isnull=True).exists()

    p = svc.record_manual_payment(blake, d, "check", 4000, dt.date(2026, 12, 28), check_number="1043")
    assert p.receipt.paid_copy_id not in (None, copy.pk)  # a new PAID copy for the new paid-in-full moment
    assert PaidCopy.objects.filter(invoice=d).count() == 2


def test_overpayment_receipt_notes_credit(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    p = svc.record_manual_payment(blake, inv, "check", 11000, dt.date(2026, 12, 20))
    assert p.receipt.kind == Receipt.Kind.PAID_IN_FULL and p.receipt.credit_cents == 300
    assert svc.receipt_summary(p.receipt)["credit"] == "$3.00"


def test_receipts_and_paid_copies_cannot_be_rewritten(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    p = svc.record_manual_payment(blake, inv, "check", 10700, dt.date(2026, 12, 20))
    svc.deliver_receipt(p.receipt, FakeRenderer(), FakeMail())
    with pytest.raises(DatabaseError), transaction.atomic():
        Receipt.objects.filter(pk=p.receipt.pk).update(balance_after_cents=5)
    with pytest.raises(DatabaseError), transaction.atomic():
        PaidCopy.objects.filter(invoice=inv).update(pdf_file="other.pdf")
    with pytest.raises(DatabaseError), transaction.atomic():
        PaidCopy.objects.filter(invoice=inv).delete()
