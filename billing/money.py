"""Billing arithmetic. Integers only: cents, basis points (700 = 7.00%), quantities in thousandths.

This module is the single source of truth for totals. The editor's live totals, the API's
"preview totals", and approval all call `compute_totals`. Floats are rejected at the door.
"""

from dataclasses import dataclass, field

BP_DENOMINATOR = 10_000  # 10000 basis points = 100%
QTY_DENOMINATOR = 1_000  # quantity 1500 = 1.5


def _require_int(name, value):
    # bool is an int subclass; reject it too so True can't sneak in as 1 cent.
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")


def div_round_half_up(numerator: int, denominator: int) -> int:
    """Integer division rounded to nearest, halves away from zero (spec: 'rounded half-up to the cent')."""
    _require_int("numerator", numerator)
    _require_int("denominator", denominator)
    if denominator <= 0:
        raise ValueError("denominator must be positive")
    if numerator < 0:
        return -div_round_half_up(-numerator, denominator)
    return (2 * numerator + denominator) // (2 * denominator)


@dataclass(frozen=True)
class LineInput:
    unit_cents: int
    qty_milli: int = QTY_DENOMINATOR
    discount_cents: int = 0
    taxable: bool = False

    def __post_init__(self):
        for name in ("unit_cents", "qty_milli", "discount_cents"):
            _require_int(name, getattr(self, name))
        if self.unit_cents < 0:
            raise ValueError("unit price can't be negative; use a discount or a credit note")
        if self.qty_milli <= 0:
            raise ValueError("quantity must be positive")
        if self.discount_cents < 0:
            raise ValueError("discount can't be negative")
        if self.discount_cents > self.gross_cents:
            raise ValueError("discount can't exceed the line amount")

    @property
    def gross_cents(self) -> int:
        return div_round_half_up(self.unit_cents * self.qty_milli, QTY_DENOMINATOR)

    @property
    def net_cents(self) -> int:
        return self.gross_cents - self.discount_cents


@dataclass(frozen=True)
class LineResult:
    gross_cents: int
    discount_cents: int
    net_cents: int
    taxable: bool
    tax_rate_bp: int  # combined
    tax_cents: int  # combined = state + local
    state_rate_bp: int = 0
    state_tax_cents: int = 0
    local_rate_bp: int = 0
    local_tax_cents: int = 0


@dataclass(frozen=True)
class Totals:
    lines: tuple = field(default_factory=tuple)
    subtotal_cents: int = 0  # sum of gross line amounts
    discount_cents: int = 0
    taxable_cents: int = 0  # net amount of taxable lines
    tax_cents: int = 0
    state_tax_cents: int = 0
    local_tax_cents: int = 0
    value_cents: int = 0  # what the work is worth (subtotal - discount + tax)
    total_cents: int = 0  # what the client owes (0 for donated work)


def line_tax(net_cents: int, rate_bp: int) -> int:
    """Tax for one line: per line, after its discount, rounded half-up (spec §4)."""
    return div_round_half_up(net_cents * rate_bp, BP_DENOMINATOR)


def split_line_tax(net_cents: int, state_bp: int, local_bp: int) -> tuple[int, int, int]:
    """(combined, state, local) for one line (spec v0.6 §4): combined and state are each rounded
    half-up; local = combined − state, so the parts always add up to what was charged."""
    combined = line_tax(net_cents, state_bp + local_bp)
    state = line_tax(net_cents, state_bp)
    return combined, state, combined - state


def compute_totals(lines, state_bp: int, local_bp: int = 0, *, customer_exempt: bool = False, donated: bool = False) -> Totals:
    _require_int("state_bp", state_bp)
    _require_int("local_bp", local_bp)
    if not (0 <= state_bp and 0 <= local_bp and state_bp + local_bp <= BP_DENOMINATOR):
        raise ValueError("tax rate out of range")
    results = []
    for line in lines:
        taxed = line.taxable and not customer_exempt and not donated and (state_bp + local_bp) > 0
        if taxed:
            combined, state, local = split_line_tax(line.net_cents, state_bp, local_bp)
        else:
            combined = state = local = 0
        results.append(
            LineResult(
                gross_cents=line.gross_cents,
                discount_cents=line.discount_cents,
                net_cents=line.net_cents,
                taxable=taxed,
                tax_rate_bp=(state_bp + local_bp) if taxed else 0,
                tax_cents=combined,
                state_rate_bp=state_bp if taxed else 0,
                state_tax_cents=state,
                local_rate_bp=local_bp if taxed else 0,
                local_tax_cents=local,
            )
        )
    subtotal = sum(r.gross_cents for r in results)
    discount = sum(r.discount_cents for r in results)
    taxable = sum(r.net_cents for r in results if r.taxable)
    tax = sum(r.tax_cents for r in results)
    value = subtotal - discount + tax
    return Totals(
        lines=tuple(results),
        subtotal_cents=subtotal,
        discount_cents=discount,
        taxable_cents=taxable,
        tax_cents=tax,
        state_tax_cents=sum(r.state_tax_cents for r in results),
        local_tax_cents=sum(r.local_tax_cents for r in results),
        value_cents=value,
        total_cents=0 if donated else value,
    )


def late_fee_cents(unpaid_cents: int, rate_bp: int, days_past_due: int, period_days: int = 30) -> int:
    """Simple late fee: rate per full period past due on the unpaid balance. Never compounding (spec §5)."""
    for name, value in (("unpaid_cents", unpaid_cents), ("rate_bp", rate_bp), ("days_past_due", days_past_due)):
        _require_int(name, value)
    if unpaid_cents <= 0 or days_past_due < period_days:
        return 0
    periods = days_past_due // period_days
    return div_round_half_up(unpaid_cents * rate_bp * periods, BP_DENOMINATOR)


def split_payment_tax(amount_cents: int, invoice_tax_cents: int, invoice_total_cents: int) -> tuple[int, int]:
    """Split a payment into (tax part, non-tax part) in the invoice's proportion (spec §4 tax report)."""
    for name, value in (("amount", amount_cents), ("tax", invoice_tax_cents), ("total", invoice_total_cents)):
        _require_int(name, value)
    if invoice_total_cents <= 0:
        return 0, amount_cents
    tax_part = div_round_half_up(amount_cents * invoice_tax_cents, invoice_total_cents)
    return tax_part, amount_cents - tax_part


def validate_online_amount(amount_cents: int, balance_cents: int, minimum_cents: int) -> None:
    """Pay page rule (spec §7): from the minimum (or the whole balance if smaller) up to the balance."""
    _require_int("amount_cents", amount_cents)
    _require_int("balance_cents", balance_cents)
    _require_int("minimum_cents", minimum_cents)
    if balance_cents <= 0:
        raise ValueError("nothing is owed on this invoice")
    if amount_cents <= 0:
        raise ValueError("amount must be positive")
    if amount_cents > balance_cents:
        raise ValueError("amount is more than the balance due")
    floor = min(minimum_cents, balance_cents)
    if amount_cents < floor:
        raise ValueError(f"the smallest payment is {format_usd(floor)}")


def format_rate(bp: int) -> str:
    """700 → '7%', 650 → '6.5%', 725 → '7.25%' (no floats)."""
    _require_int("bp", bp)
    whole, frac = divmod(bp, 100)
    return f"{whole}%" if not frac else f"{whole}.{frac:02d}".rstrip("0") + "%"


def format_usd(cents: int) -> str:
    _require_int("cents", cents)
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100:,}.{cents % 100:02d}"


def parse_usd(text: str) -> int:
    """Parse '1,234.56' or '$12' into cents without floats. Rejects more than 2 decimals."""
    s = text.strip().replace("$", "").replace(",", "")
    negative = s.startswith("-")
    if negative:
        s = s[1:]
    if not s or s.count(".") > 1:
        raise ValueError("not a dollar amount")
    whole, _, frac = s.partition(".")
    if not (whole or frac) or not (whole.isdigit() or whole == "") or not (frac.isdigit() or frac == ""):
        raise ValueError("not a dollar amount")
    if len(frac) > 2:
        raise ValueError("at most 2 decimal places")
    cents = int(whole or "0") * 100 + int((frac + "00")[:2])
    return -cents if negative else cents
