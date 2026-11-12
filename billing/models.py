"""Billing data model (spec v0.3; tech proposal v0.2 §2).

Rules enforced here AND in the database (migration 0002):
- money is integer cents; rates are basis points; quantities are thousandths
- a document gets its number only at approval, from a locked per-series/per-year counter
- anything that isn't a draft can't have its content changed or be deleted
- the audit log and payments are append-only
"""

from django.db import models
from django.db.models import Q


class Actor(models.TextChoices):
    BLAKE = "blake", "Blake"
    LEDGER = "ledger", "Ledger"
    STRIPE = "stripe", "Stripe"
    SCHEDULER = "scheduler", "Scheduler"
    IMPORT = "import", "Stripe import"
    SYSTEM = "system", "System"


class BusinessSettings(models.Model):
    """Single row (pk=1). Edited by Blake only."""

    name = models.CharField(max_length=200, default="Cosmically Creative")
    address_line1 = models.CharField(max_length=200, default="1444 1st Ave SE")
    city_state_zip = models.CharField(max_length=200, default="Sioux Center, IA 51250")
    phone = models.CharField(max_length=40, default="(712) 578-6203")
    email = models.EmailField(default="blake@cosmicallycreative.com")
    net_days = models.PositiveSmallIntegerField(default=30)
    min_online_payment_cents = models.PositiveIntegerField(default=100)  # $1.00 (Blake, 2026-11-12)
    late_fee_default_bp = models.PositiveIntegerField(default=200)  # 2% per 30 days
    send_warning_days = models.PositiveSmallIntegerField(default=7)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(pk=1), name="business_settings_singleton")]

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1)
        return obj


class StateTaxRate(models.Model):
    """Iowa's state sales tax (600 = 6.00%), with start and optional end date."""

    state = models.CharField(max_length=2, default="IA")
    rate_bp = models.PositiveIntegerField()
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)  # inclusive

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["state", "effective_from"], name="one_state_rate_per_start"),
            models.CheckConstraint(condition=Q(rate_bp__lte=10000), name="state_rate_max"),
        ]

    @classmethod
    def for_date(cls, state, day):
        rate = (
            cls.objects.filter(state=state, effective_from__lte=day)
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=day))
            .order_by("-effective_from")
            .first()
        )
        if rate is None:
            raise LookupError(f"no {state} state tax rate is in effect on {day}")
        return rate


class TaxJurisdiction(models.Model):
    """A local option jurisdiction: a city, or the rural (unincorporated) part of a county."""

    class Kind(models.TextChoices):
        CITY = "city", "City"
        RURAL_COUNTY = "rural_county", "County (rural)"

    state = models.CharField(max_length=2, default="IA")
    name = models.CharField(max_length=100)  # shown on the PDF: "Iowa + Sioux Center"
    kind = models.CharField(max_length=14, choices=Kind.choices)
    county = models.CharField(max_length=100, blank=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["state", "name", "kind"], name="unique_jurisdiction")]

    def __str__(self):
        return self.name


class LocalTaxRate(models.Model):
    """Local option rate for one jurisdiction (100 = 1.00%, or 0), with start and optional end date."""

    jurisdiction = models.ForeignKey(TaxJurisdiction, on_delete=models.PROTECT, related_name="rates")
    rate_bp = models.PositiveIntegerField()
    effective_from = models.DateField()
    effective_to = models.DateField(null=True, blank=True)  # inclusive; local options can sunset
    checked_on = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=200, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["jurisdiction", "effective_from"], name="one_local_rate_per_start"),
            models.CheckConstraint(condition=Q(rate_bp__lte=10000), name="local_rate_max"),
        ]

    @classmethod
    def bp_for(cls, jurisdiction, day):
        rate = (
            cls.objects.filter(jurisdiction=jurisdiction, effective_from__lte=day)
            .filter(Q(effective_to__isnull=True) | Q(effective_to__gte=day))
            .order_by("-effective_from")
            .first()
        )
        return rate.rate_bp if rate else 0


class Customer(models.Model):
    name = models.CharField(max_length=200)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=40, blank=True)
    address = models.TextField(blank=True)
    notes = models.TextField(blank=True)
    tax_exempt = models.BooleanField(default=False)  # Blake only
    exemption_note = models.CharField(max_length=200, blank=True)  # e.g. certificate on file
    # Confirmed tax location (where the client receives the work). Only Blake confirms it.
    tax_state = models.CharField(max_length=2, blank=True)  # "" = not set yet
    tax_jurisdiction = models.ForeignKey(TaxJurisdiction, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    tax_location_checked_on = models.DateField(null=True, blank=True)
    tax_location_source = models.CharField(max_length=200, blank=True)
    tax_location_confirmed_at = models.DateTimeField(null=True, blank=True)
    # Set when the address changed after confirmation; the confirmed location keeps applying.
    tax_location_pending = models.BooleanField(default=False)
    stripe_customer_id = models.CharField(max_length=64, blank=True)
    source = models.CharField(max_length=20, default="app")  # app | stripe_import
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.name

    @property
    def tax_location_status(self):
        if not self.tax_state:
            return "pending Blake" if self.tax_location_pending else "not set"
        return "pending Blake" if self.tax_location_pending else "confirmed"


class TaxLocationProposal(models.Model):
    """Ledger (or Blake) proposes; only Blake confirms. Nothing changes until he does."""

    class Status(models.TextChoices):
        PENDING = "pending", "Waiting for Blake"
        CONFIRMED = "confirmed", "Confirmed"
        REJECTED = "rejected", "Rejected"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="tax_proposals")
    state = models.CharField(max_length=2)
    jurisdiction = models.ForeignKey(TaxJurisdiction, null=True, blank=True, on_delete=models.PROTECT)
    address = models.TextField(blank=True)
    checked_on = models.DateField(null=True, blank=True)
    source = models.CharField(max_length=200, blank=True)
    proposed_by = models.CharField(max_length=12)
    status = models.CharField(max_length=10, choices=Status.choices, default=Status.PENDING)
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)


class Product(models.Model):
    class Kind(models.TextChoices):
        BUILD = "build", "Website build / design"
        HOSTING = "hosting", "Hosting"
        DOMAIN = "domain", "Domain"
        PLATFORM = "platform", "Platform renewal"
        DRONE = "drone", "Drone photo/video"
        LATE_FEE = "late_fee", "Late fee"
        OTHER = "other", "Other"

    name = models.CharField(max_length=200)
    kind = models.CharField(max_length=20, choices=Kind.choices, default=Kind.OTHER)
    unit_price_cents = models.PositiveIntegerField(default=0)
    default_taxable = models.BooleanField(default=False)
    active = models.BooleanField(default=True)

    def __str__(self):
        return self.name


class NumberCounter(models.Model):
    """Gapless counters. Never use database sequences for document numbers: they skip on rollback."""

    series = models.CharField(max_length=4)
    year = models.PositiveSmallIntegerField()
    next_seq = models.PositiveIntegerField(default=1)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["series", "year"], name="one_counter_per_series_year")]


class Document(models.Model):
    class Kind(models.TextChoices):
        QUOTE = "quote", "Quote"
        INVOICE = "invoice", "Invoice"
        CREDIT_NOTE = "credit_note", "Credit note"

    class Status(models.TextChoices):
        DRAFT = "draft", "Draft"
        APPROVED = "approved", "Approved"
        SENT = "sent", "Sent"
        PARTLY_PAID = "partly_paid", "Partly paid"
        PAID = "paid", "Paid"
        VOID = "void", "Void"
        # quotes only
        ACCEPTED = "accepted", "Accepted"
        DECLINED = "declined", "Declined"
        EXPIRED = "expired", "Expired"
        CONVERTED = "converted", "Converted"

    SERIES = {Kind.QUOTE: "Q", Kind.INVOICE: "CC", Kind.CREDIT_NOTE: "CN"}

    kind = models.CharField(max_length=12, choices=Kind.choices)
    status = models.CharField(max_length=12, choices=Status.choices, default=Status.DRAFT)
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="documents")

    # Assigned at approval, never before.
    series = models.CharField(max_length=4, blank=True)
    year = models.PositiveSmallIntegerField(null=True, blank=True)
    seq = models.PositiveIntegerField(null=True, blank=True)
    number = models.CharField(max_length=20, blank=True)
    issue_date = models.DateField(null=True, blank=True)
    due_date = models.DateField(null=True, blank=True)

    donated = models.BooleanField(default=False)
    late_fee_enabled = models.BooleanField(default=False)
    late_fee_bp = models.PositiveIntegerField(null=True, blank=True)
    notes = models.TextField(blank=True)  # shown on the PDF
    email_subject = models.CharField(max_length=200, blank=True)
    email_body = models.TextField(blank=True)

    # Frozen at approval.
    customer_snapshot = models.JSONField(null=True, blank=True)
    business_snapshot = models.JSONField(null=True, blank=True)
    # Per-invoice tax location override (Blake only, drafts only).
    tax_override_state = models.CharField(max_length=2, blank=True)
    tax_override_jurisdiction = models.ForeignKey(TaxJurisdiction, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    # Tax frozen at approval.
    tax_state = models.CharField(max_length=2, blank=True)
    tax_jurisdiction = models.ForeignKey(TaxJurisdiction, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    out_of_state = models.BooleanField(default=False)
    tax_label = models.CharField(max_length=120, blank=True)  # "Sales tax (7%, Iowa + Sioux Center)"
    tax_rate_bp = models.PositiveIntegerField(null=True, blank=True)  # combined
    state_rate_bp = models.PositiveIntegerField(null=True, blank=True)
    local_rate_bp = models.PositiveIntegerField(null=True, blank=True)
    state_tax_cents = models.BigIntegerField(default=0)
    local_tax_cents = models.BigIntegerField(default=0)
    subtotal_cents = models.BigIntegerField(default=0)
    discount_cents = models.BigIntegerField(default=0)
    taxable_cents = models.BigIntegerField(default=0)
    tax_cents = models.BigIntegerField(default=0)
    value_cents = models.BigIntegerField(default=0)
    total_cents = models.BigIntegerField(default=0)
    pdf_file = models.CharField(max_length=300, blank=True)
    pdf_sha256 = models.CharField(max_length=64, blank=True)
    # Unguessable code for the client pay page, printed on the PDF; set at approval (invoices only).
    pay_code = models.CharField(max_length=24, blank=True)
    pay_link_disabled = models.BooleanField(default=False)  # Blake can switch an invoice's pay link off

    prepared_by = models.CharField(max_length=12, choices=Actor.choices, default=Actor.BLAKE)
    source = models.CharField(max_length=20, default="app")  # app | stripe_import
    original_number = models.CharField(max_length=40, blank=True)  # imports keep their Stripe number
    stripe_invoice_id = models.CharField(max_length=64, blank=True)

    from_quote = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    replaces = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="replaced_by")
    credit_for = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="credit_notes")
    late_fee_for = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="late_fees")
    late_fee_period = models.PositiveSmallIntegerField(null=True, blank=True)

    approved_at = models.DateTimeField(null=True, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    sent_method = models.CharField(max_length=10, blank=True)  # email | manual
    voided_at = models.DateTimeField(null=True, blank=True)
    void_reason = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["series", "year", "seq"], condition=Q(seq__isnull=False), name="unique_document_number"),
            models.UniqueConstraint(fields=["number"], condition=~Q(number=""), name="unique_document_number_text"),
            models.UniqueConstraint(fields=["pay_code"], condition=~Q(pay_code=""), name="unique_pay_code"),
            models.UniqueConstraint(
                fields=["late_fee_for", "late_fee_period"],
                condition=Q(late_fee_for__isnull=False) & ~Q(status="void"),
                name="one_late_fee_per_period",
            ),
            models.CheckConstraint(
                condition=Q(status="draft") | Q(source="stripe_import") | Q(seq__isnull=False),
                name="approved_documents_have_numbers",
            ),
            models.CheckConstraint(condition=~Q(status="draft") | Q(seq__isnull=True), name="drafts_have_no_number"),
            models.CheckConstraint(condition=Q(total_cents__gte=0) & Q(tax_cents__gte=0), name="document_amounts_not_negative"),
            models.CheckConstraint(condition=Q(tax_cents=models.F("state_tax_cents") + models.F("local_tax_cents")), name="document_tax_parts_add_up"),
        ]
        indexes = [models.Index(fields=["kind", "status"]), models.Index(fields=["due_date"])]

    def __str__(self):
        return self.number or self.original_number or f"Draft {self.kind} #{self.pk}"

    @property
    def is_draft(self):
        return self.status == self.Status.DRAFT

    @property
    def display_number(self):
        return self.number or self.original_number


class LineItem(models.Model):
    document = models.ForeignKey(Document, on_delete=models.CASCADE, related_name="lines")
    position = models.PositiveSmallIntegerField(default=0)
    product = models.ForeignKey(Product, null=True, blank=True, on_delete=models.PROTECT)
    description = models.CharField(max_length=500)
    qty_milli = models.PositiveIntegerField(default=1000)
    unit_cents = models.PositiveIntegerField()
    discount_cents = models.PositiveIntegerField(default=0)
    discount_label = models.CharField(max_length=200, blank=True)
    taxable = models.BooleanField(default=False)
    # Filled at approval.
    line_cents = models.BigIntegerField(default=0)
    tax_rate_bp = models.PositiveIntegerField(default=0)  # combined
    tax_cents = models.BigIntegerField(default=0)  # combined = state + local
    state_rate_bp = models.PositiveIntegerField(default=0)
    state_tax_cents = models.BigIntegerField(default=0)
    local_rate_bp = models.PositiveIntegerField(default=0)
    local_tax_cents = models.BigIntegerField(default=0)
    tax_jurisdiction = models.ForeignKey(TaxJurisdiction, null=True, blank=True, on_delete=models.PROTECT, related_name="+")

    class Meta:
        ordering = ["position", "pk"]
        constraints = [
            models.CheckConstraint(condition=Q(qty_milli__gt=0), name="line_qty_positive"),
            models.CheckConstraint(condition=Q(tax_cents=models.F("state_tax_cents") + models.F("local_tax_cents")), name="line_tax_parts_add_up"),
        ]


class Payment(models.Model):
    class Method(models.TextChoices):
        STRIPE = "stripe", "Online (Stripe)"
        CHECK = "check", "Check"
        CASH = "cash", "Cash"
        OTHER = "other", "Other"

    invoice = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="payments")
    method = models.CharField(max_length=10, choices=Method.choices)
    amount_cents = models.BigIntegerField()  # what was received
    applied_cents = models.BigIntegerField()  # what went against this invoice; the rest is customer credit
    stripe_fee_cents = models.BigIntegerField(default=0)
    received_on = models.DateField()
    check_number = models.CharField(max_length=40, blank=True)
    note = models.CharField(max_length=300, blank=True)
    stripe_payment_intent_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    stripe_event_id = models.CharField(max_length=64, blank=True)
    recorded_by = models.CharField(max_length=12, choices=Actor.choices)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.CheckConstraint(condition=Q(amount_cents__gt=0), name="payment_positive"),
            models.CheckConstraint(
                condition=Q(applied_cents__gte=0) & Q(applied_cents__lte=models.F("amount_cents")),
                name="payment_applied_within_amount",
            ),
            models.CheckConstraint(condition=Q(stripe_fee_cents__gte=0), name="payment_fee_not_negative"),
            # Online payments come only from Stripe; manual ones only from Blake.
            models.CheckConstraint(
                condition=(Q(method="stripe") & Q(recorded_by="stripe")) | (~Q(method="stripe") & Q(recorded_by__in=["blake", "import"])),
                name="payment_recorded_by_right_actor",
            ),
        ]


class Refund(models.Model):
    class Resolution(models.TextChoices):
        PENDING = "pending", "Waiting for Blake"
        CREDIT_NOTE = "credit_note", "Credit note (nothing owed)"
        REOPEN = "reopen", "Reopen balance"

    payment = models.ForeignKey(Payment, on_delete=models.PROTECT, related_name="refunds")
    amount_cents = models.BigIntegerField()
    stripe_refund_id = models.CharField(max_length=64, null=True, blank=True, unique=True)
    recorded_by = models.CharField(max_length=12, choices=Actor.choices)
    resolution = models.CharField(max_length=12, choices=Resolution.choices, default=Resolution.PENDING)
    credit_note = models.ForeignKey(Document, null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        constraints = [models.CheckConstraint(condition=Q(amount_cents__gt=0), name="refund_positive")]


class CustomerCredit(models.Model):
    """Ledger of a customer's credit: + from overpayments, − when applied. Append-only."""

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="credits")
    amount_cents = models.BigIntegerField()
    source_payment = models.ForeignKey(Payment, null=True, blank=True, on_delete=models.PROTECT)
    note = models.CharField(max_length=300, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)


class StripeEvent(models.Model):
    """Every verified Stripe notification, stored once. A duplicate delivery is a no-op."""

    event_id = models.CharField(max_length=64, primary_key=True)
    type = models.CharField(max_length=80)
    payload = models.JSONField()
    received_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    result = models.CharField(max_length=200, blank=True)


class RecurringTemplate(models.Model):
    class Interval(models.TextChoices):
        YEARLY = "yearly", "Yearly"
        MONTHLY = "monthly", "Monthly"

    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="recurring")
    lines = models.JSONField()  # [{"description", "unit_cents", "qty_milli", "taxable", "product_id"}]
    interval = models.CharField(max_length=10, choices=Interval.choices, default=Interval.YEARLY)
    next_date = models.DateField()
    lead_days = models.PositiveSmallIntegerField(default=14)
    active = models.BooleanField(default=True)


class RecurringRun(models.Model):
    template = models.ForeignKey(RecurringTemplate, on_delete=models.PROTECT, related_name="runs")
    period_start = models.DateField()
    # Kept if Blake deletes the draft, so the same renewal is never drafted twice.
    draft = models.ForeignKey(Document, null=True, on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [models.UniqueConstraint(fields=["template", "period_start"], name="one_draft_per_period")]


class ApiToken(models.Model):
    """Ledger's token. Only a SHA-256 hash is stored; the token itself is shown once."""

    name = models.CharField(max_length=100)
    actor = models.CharField(max_length=12, choices=Actor.choices, default=Actor.LEDGER)
    token_hash = models.CharField(max_length=64, unique=True)
    prefix = models.CharField(max_length=12)  # first characters, for recognising it in Settings
    scopes = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)


class AuditEvent(models.Model):
    """Permanent change history. Rows can only be inserted (database trigger), and each row's hash
    covers the previous row's hash, so removing or altering history is detectable."""

    at = models.DateTimeField()
    actor = models.CharField(max_length=12, choices=Actor.choices)
    actor_ref = models.CharField(max_length=64, blank=True)  # user id or token prefix
    action = models.CharField(max_length=60)
    entity = models.CharField(max_length=40)
    entity_id = models.CharField(max_length=40)
    before = models.JSONField(null=True, blank=True)
    after = models.JSONField(null=True, blank=True)
    note = models.CharField(max_length=500, blank=True)
    prev_hash = models.CharField(max_length=64)
    hash = models.CharField(max_length=64, unique=True)

    class Meta:
        ordering = ["id"]
        indexes = [models.Index(fields=["entity", "entity_id"])]


class PaidCopy(models.Model):
    """The PAID-stamped copy of an invoice, made once when its balance reaches $0 and then stored.
    The original PDF from approval never changes. If a refund reopens the balance, this copy is
    marked superseded (never deleted) and the next paid-in-full moment makes a new one."""

    invoice = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="paid_copies")
    paid_in_full_on = models.DateField()
    # Frozen list of every payment and credit applied, for the stamp.
    applied = models.JSONField()
    pdf_file = models.CharField(max_length=300, blank=True)  # set once, when rendered
    pdf_sha256 = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    superseded_at = models.DateTimeField(null=True, blank=True)
    superseded_reason = models.CharField(max_length=300, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["invoice"], condition=Q(superseded_at__isnull=True), name="one_current_paid_copy"),
        ]


class Receipt(models.Model):
    """Exactly one receipt per payment. Created in the payment's transaction; emailed afterwards,
    so a mail failure never undoes a payment."""

    class Kind(models.TextChoices):
        PARTIAL = "partial", "Partial payment"
        PAID_IN_FULL = "paid_in_full", "Paid in full"

    class EmailStatus(models.TextChoices):
        PENDING = "pending", "Waiting to send"
        SENT = "sent", "Sent"
        FAILED = "failed", "Failed"
        NOT_SENT = "not_sent", "Not sent (Blake unticked it)"

    payment = models.OneToOneField(Payment, on_delete=models.PROTECT, related_name="receipt")
    kind = models.CharField(max_length=14, choices=Kind.choices)
    paid_copy = models.ForeignKey(PaidCopy, null=True, blank=True, on_delete=models.PROTECT, related_name="receipts")
    paid_so_far_cents = models.BigIntegerField()
    balance_after_cents = models.BigIntegerField()
    credit_cents = models.BigIntegerField(default=0)  # overpayment that became customer credit
    email_status = models.CharField(max_length=10, choices=EmailStatus.choices)
    recipients = models.JSONField(default=list)
    attempts = models.PositiveSmallIntegerField(default=0)
    last_error = models.CharField(max_length=300, blank=True)
    sent_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
