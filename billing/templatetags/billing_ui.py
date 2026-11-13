from django import template
from billing.money import format_usd
register = template.Library()
@register.filter
def usd(value):
    return format_usd(value) if isinstance(value, int) else '—'

@register.filter
def quantity(value):
    from billing.pdf import fmt_qty
    return fmt_qty(value)
