"""Shared fixtures. All names and amounts are FAKE sample data, never real clients."""

import datetime as dt
import zoneinfo

import pytest

from billing import services as svc
from billing.models import Customer, Product
from billing.permissions import ActorRef

CENTRAL = zoneinfo.ZoneInfo("America/Chicago")


def central(y, m, d, hh=12, mm=0, ss=0):
    return dt.datetime(y, m, d, hh, mm, ss, tzinfo=CENTRAL)


@pytest.fixture
def blake():
    return ActorRef.blake("1")


@pytest.fixture
def ledger():
    return ActorRef.ledger("ccb_testtok")


@pytest.fixture
def customer(db, blake):
    return svc.create_customer(blake, name="Sample Bakery (fake)", email="owner@example.com")


@pytest.fixture
def exempt_customer(db, blake):
    return svc.create_customer(blake, name="Sample Church (fake)", email="office@example.org", tax_exempt=True, exemption_note="certificate on file (sample)")


@pytest.fixture
def products(db):
    return {
        "build": Product.objects.get(name="Website build"),
        "hosting": Product.objects.get(name="Web hosting (annual)"),
        "domain": Product.objects.get(name="Domain registration (annual)"),
    }


def line(product=None, cents=0, **kw):
    data = {"unit_cents": cents, **kw}
    if product is not None:
        data["product"] = product
    return data


@pytest.fixture
def make_invoice(db, blake, customer, products):
    """Draft → approved (→ sent) invoice with the given lines; returns the fresh object."""

    def _make(lines=None, cust=None, sent=True, when=None, **fields):
        lines = lines or [line(products["build"], 10000)]
        doc = svc.create_draft(blake, "invoice", cust or customer, lines, **fields)
        doc = svc.approve(blake, doc, now=when or central(2026, 12, 10))
        if sent:
            svc.mark_sent(blake, doc, "manual", now=when or central(2026, 12, 10))
        doc.refresh_from_db()
        return doc

    return _make
