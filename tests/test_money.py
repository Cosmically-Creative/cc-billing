"""Pure arithmetic: integers only, half-up rounding, per-line tax."""

import pytest

from billing import money
from billing.money import LineInput, compute_totals, div_round_half_up


@pytest.mark.parametrize(
    "n,d,expected",
    [(5, 10, 1), (4, 10, 0), (15, 10, 2), (25, 10, 3), (-5, 10, -1), (-4, 10, 0), (2333100, 10000, 233), (5000, 10000, 1)],
)
def test_round_half_up(n, d, expected):
    assert div_round_half_up(n, d) == expected


def test_floats_are_rejected_everywhere():
    with pytest.raises(TypeError):
        LineInput(unit_cents=10.5)
    with pytest.raises(TypeError):
        LineInput(unit_cents=100, qty_milli=1.5)
    with pytest.raises(TypeError):
        compute_totals([LineInput(100)], 7.0)
    with pytest.raises(TypeError):
        money.validate_online_amount(10.0, 100, 100)
    with pytest.raises(TypeError):
        div_round_half_up(1.0, 3)
    with pytest.raises(TypeError):
        LineInput(unit_cents=True)  # bools aren't cents


def test_fractional_quantity_without_floats():
    # 1.5 hours at $85.33 = $127.995 → $128.00 (half-up)
    assert LineInput(unit_cents=8533, qty_milli=1500).gross_cents == 12800


def test_discount_reduces_taxable_amount():
    t = compute_totals([LineInput(unit_cents=90000, discount_cents=10000, taxable=True)], 700)
    assert (t.subtotal_cents, t.discount_cents, t.taxable_cents, t.tax_cents, t.total_cents) == (90000, 10000, 80000, 5600, 85600)


def test_discount_cannot_exceed_line():
    with pytest.raises(ValueError):
        LineInput(unit_cents=100, discount_cents=101)


def test_donated_shows_value_but_owes_nothing():
    t = compute_totals([LineInput(65000, taxable=True)], 700, donated=True)
    assert t.value_cents == 65000 and t.total_cents == 0 and t.tax_cents == 0


def test_late_fee_simple_not_compounding():
    assert money.late_fee_cents(50000, 200, 29) == 0
    assert money.late_fee_cents(50000, 200, 31) == 1000
    assert money.late_fee_cents(50000, 200, 61) == 2000  # 2 periods, simple


def test_payment_tax_split():
    # $107 invoice with $7 tax; a $40 payment carries $2.62 of tax
    assert money.split_payment_tax(4000, 700, 10700) == (262, 3738)


@pytest.mark.parametrize("text,cents", [("12", 1200), ("$1,234.56", 123456), ("0.5", 50), (".99", 99), ("-3.10", -310)])
def test_parse_usd(text, cents):
    assert money.parse_usd(text) == cents


@pytest.mark.parametrize("text", ["", ".", "1.234", "abc", "1.2.3", "$"])
def test_parse_usd_rejects(text):
    with pytest.raises(ValueError):
        money.parse_usd(text)


def test_format_usd():
    assert money.format_usd(376316) == "$3,763.16"
    assert money.format_usd(-4000) == "-$40.00"
