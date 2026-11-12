"""Test 19 (December part): import only customers with invoices. FAKE export rows."""

import pytest

from billing.importer import import_stripe_customers
from billing.models import AuditEvent, Customer
from billing.permissions import ActorRef, PermissionDenied, RuleError

FAKE = """stripe_customer_id,name,tax_exempt,invoices_in_stripe,note
cus_FAKE0000000001,"Sample Pantry (fake)",no,1,
cus_FAKE0000000002,"Sample Church, Onawa (fake)",yes,1,address in Stripe
cus_FAKE0000000003,Sample Detailing (fake),no,3,
cus_FAKE0000000004,"Sample Hands (fake)",no,2,
cus_FAKE0000000005,"Sample Love, Hope, and Joy (fake)",no,1,
cus_FAKE0000000006,Sample Nails (fake),no,0,
cus_FAKE0000000007,Sample Quilting (fake),no,0,
cus_FAKE0000000008,Sample Box (fake),no,0,quote only
"""


@pytest.fixture
def export(tmp_path):
    p = tmp_path / "customers.csv"
    p.write_text(FAKE, encoding="utf-8")
    return p


def test_19_dec_only_customers_with_invoices(db, export):
    r = import_stripe_customers(export)
    assert len(r["imported"]) == 5 and len(r["skipped_no_invoices"]) == 3
    assert not Customer.objects.filter(name__in=["Sample Nails (fake)", "Sample Quilting (fake)"]).exists()
    church = Customer.objects.get(stripe_customer_id="cus_FAKE0000000002")
    assert church.name == "Sample Church, Onawa (fake)" and church.tax_exempt and church.source == "stripe_import"
    assert church.tax_location_status == "not set"  # Ledger proposes, Blake confirms
    assert AuditEvent.objects.filter(actor="import", action="import").count() == 5


def test_import_is_safe_to_rerun(db, export):
    import_stripe_customers(export)
    r = import_stripe_customers(export)
    assert r["imported"] == [] and len(r["skipped_existing"]) == 5
    assert Customer.objects.count() == 5


def test_import_needs_the_import_actor_and_valid_file(db, export, tmp_path):
    with pytest.raises(PermissionDenied):
        import_stripe_customers(export, actor=ActorRef.ledger())
    bad = tmp_path / "bad.csv"
    bad.write_text("name\nSomeone\n", encoding="utf-8")
    with pytest.raises(RuleError):
        import_stripe_customers(bad)
