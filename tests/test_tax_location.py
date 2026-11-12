"""Sales tax by the client's location (billing spec v0.6 §4, tests 26–31). Fake customers; the
"Nowhere Township" jurisdiction is invented for the tests."""

import datetime as dt

import pytest

from billing import services as svc
from billing.models import AuditEvent, LocalTaxRate, NumberCounter, TaxJurisdiction
from billing.permissions import PermissionDenied, RuleError

from .conftest import central, jurisdiction, line


def test_26_state_and_local_split_stored_per_line(make_invoice, products):
    inv = make_invoice([line(products["build"], 10000), line(products["build"], 3333)])
    a, b = inv.lines.all()
    assert (a.tax_cents, a.state_tax_cents, a.local_tax_cents) == (700, 600, 100)
    assert (b.tax_cents, b.state_tax_cents, b.local_tax_cents) == (233, 200, 33)
    assert a.tax_jurisdiction.name == "Sioux Center" and (a.state_rate_bp, a.local_rate_bp) == (600, 100)
    assert (inv.state_tax_cents, inv.local_tax_cents, inv.tax_cents) == (800, 133, 933)
    assert inv.tax_label == "Sales tax (7%, Iowa + Sioux Center)"


@pytest.fixture
def nowhere(db):
    j = TaxJurisdiction.objects.create(state="IA", name="Nowhere Township (fake)", kind="rural_county")
    LocalTaxRate.objects.create(jurisdiction=j, rate_bp=0, effective_from=dt.date(2026, 1, 1))
    return j


def test_27_no_local_option_and_out_of_state(blake, make_invoice, products, nowhere):
    c6 = svc.create_customer(blake, name="Sample Farm (fake)", email="farm@example.com")
    svc.confirm_tax_location(blake, c6, "IA", nowhere)
    inv = make_invoice([line(products["build"], 10000)], cust=c6)
    assert (inv.tax_cents, inv.local_tax_cents) == (600, 0) and inv.tax_label == "Sales tax (6%, Iowa)"

    away = svc.create_customer(blake, name="Sample Studio, Omaha (fake)", email="studio@example.com")
    svc.confirm_tax_location(blake, away, "NE")
    inv = make_invoice([line(products["build"], 10000)], cust=away)
    assert inv.tax_cents == 0 and inv.out_of_state and inv.total_cents == 10000
    assert inv.tax_label == "No Iowa sales tax (out of state)"


def test_28_no_tax_location_blocks_taxed_invoice_only(blake, products):
    c = svc.create_customer(blake, name="Sample Shop (fake)", email="shop@example.com")
    taxed = svc.create_draft(blake, "invoice", c, [line(products["build"], 10000)])
    with pytest.raises(RuleError, match="Add the client's address first"):
        svc.approve(blake, taxed, now=central(2026, 12, 10))
    assert not NumberCounter.objects.filter(series="CC", next_seq__gt=1).exists()  # no number used
    _, ctx = svc.preview_totals(taxed, dt.date(2026, 12, 10))
    assert ctx.missing  # the editor shows the warning

    renewal = svc.create_draft(blake, "invoice", c, [line(products["hosting"], 6000), line(products["domain"], 1500)])
    renewal = svc.approve(blake, renewal, now=central(2026, 12, 10))
    assert renewal.number == "CC-2026-0001" and renewal.tax_cents == 0


def test_29_local_rate_end_date(blake, products):
    j = TaxJurisdiction.objects.create(state="IA", name="Sunset City (fake)", kind="city")
    LocalTaxRate.objects.create(jurisdiction=j, rate_bp=100, effective_from=dt.date(2026, 1, 1), effective_to=dt.date(2026, 12, 31))
    c = svc.create_customer(blake, name="Sample Cafe (fake)", email="cafe@example.com")
    svc.confirm_tax_location(blake, c, "IA", j)
    a = svc.approve(blake, svc.create_draft(blake, "invoice", c, [line(products["build"], 10000)]), now=central(2026, 12, 31, 15))
    b = svc.approve(blake, svc.create_draft(blake, "invoice", c, [line(products["build"], 10000)]), now=central(2027, 1, 1, 9))
    assert (a.tax_cents, a.tax_label) == (700, "Sales tax (7%, Iowa + Sunset City (fake))")
    assert (b.tax_cents, b.tax_label) == (600, "Sales tax (6%, Iowa)")
    a.refresh_from_db()
    assert a.tax_cents == 700  # the Dec 31 invoice never changes


def test_30_ledger_proposes_only(blake, ledger, customer, products):
    svc.update_customer(ledger, customer, address="5 Sample Ave, Orange City, IA")
    customer.refresh_from_db()
    assert customer.address.startswith("5 Sample Ave") and customer.tax_location_status == "pending Blake"
    prop = svc.propose_tax_location(ledger, customer, "IA", jurisdiction("Orange City"), checked_on=dt.date(2026, 12, 1), source="DOR map")
    d = svc.approve(blake, svc.create_draft(ledger, "invoice", customer, [line(products["build"], 10000)]), now=central(2026, 12, 10))
    assert d.tax_jurisdiction.name == "Sioux Center"  # still the confirmed location
    with pytest.raises(PermissionDenied):
        svc.confirm_tax_location(ledger, customer, proposal=prop)
    assert AuditEvent.objects.filter(actor="ledger", action="denied:confirm_tax_location").exists()
    with pytest.raises(PermissionDenied):
        svc.update_draft(ledger, svc.create_draft(ledger, "invoice", customer, [line(products["build"], 100)]), tax_override_state="NE")
    svc.confirm_tax_location(blake, customer, proposal=prop)
    customer.refresh_from_db()
    assert customer.tax_jurisdiction.name == "Orange City" and customer.tax_location_status == "confirmed"


def test_override_is_blake_only_and_applies_to_one_invoice(blake, customer, products):
    d = svc.create_draft(blake, "invoice", customer, [line(products["build"], 10000)], tax_override_state="IA", tax_override_jurisdiction=jurisdiction("Hull"))
    d = svc.approve(blake, d, now=central(2026, 12, 10))
    assert d.tax_jurisdiction.name == "Hull"
    customer.refresh_from_db()
    assert customer.tax_jurisdiction.name == "Sioux Center"


def test_credit_note_uses_original_rates(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 15000)])
    cn = svc.create_credit_note_draft(blake, inv, [line(products["build"], 15000)])
    cn = svc.approve(blake, cn, now=central(2026, 12, 15))
    assert (cn.tax_cents, cn.state_tax_cents, cn.local_tax_cents) == (inv.tax_cents, inv.state_tax_cents, inv.local_tax_cents)
    assert cn.tax_jurisdiction_id == inv.tax_jurisdiction_id


def test_31_tax_report_by_jurisdiction(blake, customer, exempt_customer, make_invoice, products):
    onawa = svc.create_customer(blake, name="Sample Diner (fake)", email="diner@example.com")
    svc.confirm_tax_location(blake, onawa, "IA", jurisdiction("Onawa"))
    i1 = make_invoice([line(products["build"], 10000), line(products["hosting"], 6000)])  # Sioux Center
    i2 = make_invoice([line(products["build"], 3333)], cust=onawa)
    i3 = make_invoice([line(products["build"], 65000)], cust=exempt_customer)
    svc.void(blake, make_invoice([line(products["build"], 99999)]), "sample")  # voided: not reported
    rep = svc.tax_report(dt.date(2026, 12, 1), dt.date(2026, 12, 31))
    rows = {r["jurisdiction"]: r for r in rep["jurisdictions"]}
    assert set(rows) == {"Sioux Center", "Onawa"}
    assert (rows["Sioux Center"]["taxable_cents"], rows["Sioux Center"]["state_tax_cents"], rows["Sioux Center"]["local_tax_cents"]) == (10000, 600, 100)
    assert (rows["Onawa"]["taxable_cents"], rows["Onawa"]["state_tax_cents"], rows["Onawa"]["local_tax_cents"]) == (3333, 200, 33)
    assert rep["exempt_sales"] == [{"customer": "Sample Church (fake)", "sales_cents": 65000}]
    assert rep["non_taxable_cents"] == 6000
    assert rep["invoiced"]["tax_cents"] == i1.tax_cents + i2.tax_cents + i3.tax_cents == 933


def test_tax_received_split(blake, make_invoice, products):
    inv = make_invoice([line(products["build"], 10000)])  # $107.00 incl. $6 state + $1 local
    svc.record_manual_payment(blake, inv, "check", 10700, dt.date(2026, 12, 20))
    rec = svc.tax_report(dt.date(2026, 12, 1), dt.date(2026, 12, 31))["received"]
    assert (rec["tax_cents"], rec["state_tax_cents"], rec["local_tax_cents"], rec["other_cents"]) == (700, 600, 100, 10000)


def test_tax_fields_are_locked_after_approval(make_invoice, products):
    from django.db import DatabaseError, transaction

    from billing.models import Document

    inv = make_invoice([line(products["build"], 10000)])
    for change in ({"local_tax_cents": 0, "state_tax_cents": 700}, {"tax_label": "Sales tax (6%, Iowa)"}, {"tax_jurisdiction": None}):
        with pytest.raises(DatabaseError), transaction.atomic():
            Document.objects.filter(pk=inv.pk).update(**change)
