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


DATA = """name,stripe_customer_id,create_if_missing,state,jurisdiction,jurisdiction_kind,tax_exempt,checked_on,source
Sample Pantry (fake),cus_FAKE0000000001,no,IA,Sioux Center,city,,2026-11-12,Blake in chat (fake)
"Sample Church, Onawa (fake)",cus_FAKE0000000002,no,IA,Onawa,city,yes,2026-11-12,Blake in chat (fake)
Sample Box (fake),cus_FAKE0000000008,yes,IA,Orange City,city,,2026-11-12,Blake in chat (fake)
Sample Farm (fake),,yes,IA,Sioux Center,city,,2026-11-12,Blake in chat (fake)
Sample Nowhere (fake),,no,IA,Sioux Center,city,,2026-11-12,should be skipped
"""


def test_v07_customer_data_step(db, export, tmp_path, products):
    from billing import services as svc
    from billing.importer import apply_customer_data

    import_stripe_customers(export)
    data = tmp_path / "data.csv"
    data.write_text(DATA, encoding="utf-8")
    result = dict(apply_customer_data(data))
    assert result["Sample Pantry (fake)"] == "location confirmed: Sioux Center"
    assert result["Sample Box (fake)"].startswith("created") and result["Sample Farm (fake)"].startswith("created")
    assert result["Sample Nowhere (fake)"] == "not found; skipped"
    box = Customer.objects.get(name="Sample Box (fake)")
    assert box.stripe_customer_id == "cus_FAKE0000000008" and box.tax_jurisdiction.name == "Orange City"
    assert box.tax_location_status == "confirmed" and box.tax_location_source == "Blake in chat (fake)"
    church = Customer.objects.get(stripe_customer_id="cus_FAKE0000000002")
    assert church.tax_exempt and church.tax_jurisdiction.name == "Onawa"
    assert AuditEvent.objects.filter(actor="blake", action="confirm_tax_location").count() == 4
    # re-run changes nothing
    again = dict(apply_customer_data(data))
    assert again["Sample Pantry (fake)"] == "location already confirmed"
    assert Customer.objects.filter(name="Sample Farm (fake)").count() == 1
    # a taxed quote for the new customer is no longer blocked
    q = svc.create_draft(svc.ActorRef.blake(), "quote", box, [{"product": products["build"], "unit_cents": 82500}])
    q = svc.approve(svc.ActorRef.blake(), q)
    assert q.tax_label == "Sales tax (7%, Iowa + Orange City)" and q.tax_cents == 5775


def test_drone_is_taxable_by_default(db):
    from billing.models import Product

    assert Product.objects.get(name="Drone photo/video").default_taxable
