"""Who may do what (spec v0.3 §8). Every service function checks here first, so screens, the API and
the MCP layer all share one set of rules. Hiding a button is never the only protection."""

from dataclasses import dataclass, field

from .models import Actor


class BillingError(Exception):
    """Base for refusals the user should see."""


class PermissionDenied(BillingError):
    pass


class LockedError(BillingError):
    pass


class RuleError(BillingError):
    """A billing rule was broken (bad amount, wrong state, ...)."""


@dataclass(frozen=True)
class ActorRef:
    kind: str  # an Actor value
    ref: str = ""  # user id, or token prefix
    scopes: frozenset = field(default_factory=frozenset)

    @classmethod
    def blake(cls, user_id="owner"):
        return cls(Actor.BLAKE, str(user_id))

    @classmethod
    def ledger(cls, prefix="ledger", scopes=("read", "drafts:write")):
        return cls(Actor.LEDGER, prefix, frozenset(scopes))

    @classmethod
    def stripe(cls):
        return cls(Actor.STRIPE, "webhook")

    @classmethod
    def scheduler(cls):
        return cls(Actor.SCHEDULER, "daily")


B, L, S, SCH, IMP = Actor.BLAKE, Actor.LEDGER, Actor.STRIPE, Actor.SCHEDULER, Actor.IMPORT

CAPABILITIES = {
    "view": {B, L},
    "create_customer": {B, L},
    "edit_customer_contact": {B, L},
    "edit_customer_tax": {B},
    "edit_settings": {B},
    "edit_draft": {B, L},
    "edit_billing_terms": {B},  # donated, late-fee switch/rate
    "override_taxable": {B},  # change a line's taxable flag away from the product default
    "convert_quote": {B, L},
    "draft_late_fee": {B, L},
    "approve": {B},
    "send": {B},
    "quote_outcome": {B},
    "record_manual_payment": {B},
    "resolve_refund": {B},
    "resend_receipt": {B},
    "record_online_payment": {S},
    "void": {B},
    "credit_note": {B},
    "delete_draft": {B},
    "edit_recurring": {B},
    "create_recurring_draft": {B, SCH},
    "manage_tokens": {B},
    "import": {IMP},
}

# A Ledger token additionally needs the matching scope.
TOKEN_SCOPE = {
    "view": "read",
    "create_customer": "drafts:write",
    "edit_customer_contact": "drafts:write",
    "edit_draft": "drafts:write",
    "convert_quote": "drafts:write",
    "draft_late_fee": "drafts:write",
}


def allowed(actor: ActorRef, capability: str) -> bool:
    if capability not in CAPABILITIES:
        raise KeyError(f"unknown capability {capability}")
    if actor.kind not in CAPABILITIES[capability]:
        return False
    if actor.kind == Actor.LEDGER:
        scope = TOKEN_SCOPE.get(capability)
        return scope is not None and scope in actor.scopes
    return True


def require(actor: ActorRef, capability: str, entity: str = "", entity_id="") -> None:
    """Raise PermissionDenied and log the attempt. Call this before opening the change's transaction,
    so the refusal entry is kept even though the change itself never happens."""
    if allowed(actor, capability):
        return
    from . import audit

    audit.record(actor, f"denied:{capability}", entity or "-", entity_id or "-", note="refused")
    raise PermissionDenied(f"{actor.kind} may not {capability.replace('_', ' ')}")
