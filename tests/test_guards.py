"""The database itself protects history, payments and numbering, not just the app code."""

import datetime as dt
import threading

import pytest
from django.db import DatabaseError, connection, connections, transaction

from billing import audit, services as svc
from billing.models import AuditEvent, Document, Payment

from .conftest import central, line


def test_history_is_append_only(blake, customer):
    ev = AuditEvent.objects.filter(entity="customer").first()
    with pytest.raises(DatabaseError), transaction.atomic():
        AuditEvent.objects.filter(pk=ev.pk).update(note="rewritten")
    with pytest.raises(DatabaseError), transaction.atomic():
        AuditEvent.objects.filter(pk=ev.pk).delete()


def test_tampering_is_detected(blake, customer):
    svc.update_customer(blake, customer, phone="555-0100")
    assert audit.verify_chain()[0]
    with connection.cursor() as cur:  # simulate someone with superuser access bypassing the trigger
        cur.execute("ALTER TABLE billing_auditevent DISABLE TRIGGER ccb_audit_append_only")
        cur.execute("UPDATE billing_auditevent SET note = 'edited' WHERE id = (SELECT min(id) FROM billing_auditevent)")
        cur.execute("ALTER TABLE billing_auditevent ENABLE TRIGGER ccb_audit_append_only")
    ok, message = audit.verify_chain()
    assert not ok and "don't match" in message


def test_payments_are_append_only(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    p = svc.record_manual_payment(blake, inv, "check", 1000, dt.date(2026, 12, 11))
    with pytest.raises(DatabaseError), transaction.atomic():
        Payment.objects.filter(pk=p.pk).update(amount_cents=1)
    with pytest.raises(DatabaseError), transaction.atomic():
        Payment.objects.filter(pk=p.pk).delete()


def test_void_is_final_and_nothing_returns_to_draft(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    with pytest.raises(DatabaseError), transaction.atomic():
        Document.objects.filter(pk=inv.pk).update(status="draft")
    svc.void(blake, inv, "sample")
    with pytest.raises(DatabaseError), transaction.atomic():
        Document.objects.filter(pk=inv.pk).update(status="sent")


def test_stripe_payment_needs_stripe_actor(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])
    with pytest.raises(DatabaseError), transaction.atomic():
        Payment.objects.create(invoice=inv, method="stripe", amount_cents=100, applied_cents=100, received_on=dt.date(2026, 12, 11), recorded_by="blake")
    with pytest.raises(svc.RuleError):
        svc.record_manual_payment(blake, inv, "stripe", 100, dt.date(2026, 12, 11))


@pytest.mark.django_db(transaction=True, serialized_rollback=True)
def test_concurrent_approvals_get_distinct_numbers_without_gaps(customer, products, blake):
    drafts = [svc.create_draft(blake, "invoice", customer, [line(products["build"], 1000)]) for _ in range(6)]
    barrier = threading.Barrier(len(drafts))
    errors = []

    def worker(doc):
        try:
            barrier.wait()
            svc.approve(blake, doc, now=central(2026, 12, 10))
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)
        finally:
            connections.close_all()

    threads = [threading.Thread(target=worker, args=(d,)) for d in drafts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    numbers = sorted(Document.objects.filter(kind="invoice").values_list("seq", flat=True))
    assert numbers == [1, 2, 3, 4, 5, 6]
    assert audit.verify_chain()[0]
