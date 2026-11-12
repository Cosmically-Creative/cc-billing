"""Database-level guards. These hold even if application code has a bug, or someone edits rows by hand.

1. Approved documents are locked: content columns can't change, status can't go back to draft,
   void is final, and only drafts can be deleted.
2. Line items can only be added, changed or removed while their document is a draft.
3. The audit log, payments and customer credits are append-only.
4. Refunds can only be decided once; Stripe events can only gain a processed result.

TRUNCATE is not covered by row triggers; at deploy time the app's database role has no TRUNCATE,
UPDATE or DELETE rights on the append-only tables (see docs/deploy.md).
"""

from django.db import migrations

LOCKED_COLUMNS = [
    "kind",
    "customer_id",
    "series",
    "year",
    "seq",
    "number",
    "issue_date",
    "due_date",
    "donated",
    "late_fee_enabled",
    "late_fee_bp",
    "notes",
    "customer_snapshot",
    "business_snapshot",
    "tax_rate_bp",
    "subtotal_cents",
    "discount_cents",
    "taxable_cents",
    "tax_cents",
    "value_cents",
    "total_cents",
    "pdf_file",
    "pdf_sha256",
    "prepared_by",
    "source",
    "original_number",
    "stripe_invoice_id",
    "from_quote_id",
    "replaces_id",
    "credit_for_id",
    "late_fee_for_id",
    "late_fee_period",
    "approved_at",
]

_old = ", ".join(f"OLD.{c}" for c in LOCKED_COLUMNS)
_new = ", ".join(f"NEW.{c}" for c in LOCKED_COLUMNS)

FORWARD = f"""
CREATE OR REPLACE FUNCTION ccb_document_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN
      RAISE EXCEPTION 'document % (%) is not a draft and cannot be deleted', OLD.id, OLD.number
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN OLD;
  END IF;
  IF OLD.status <> 'draft' THEN
    IF ROW({_old}) IS DISTINCT FROM ROW({_new}) THEN
      RAISE EXCEPTION 'document % (%) is locked; void it or issue a credit note', OLD.id, OLD.number
        USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'draft' THEN
      RAISE EXCEPTION 'document % cannot go back to draft', OLD.number USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'void' AND NEW.status <> 'void' THEN
      RAISE EXCEPTION 'document % is void; void is final', OLD.number USING ERRCODE = 'integrity_constraint_violation';
    END IF;
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER ccb_document_guard BEFORE UPDATE OR DELETE ON billing_document
  FOR EACH ROW EXECUTE FUNCTION ccb_document_guard();

CREATE OR REPLACE FUNCTION ccb_line_guard() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE st text;
BEGIN
  SELECT status INTO st FROM billing_document WHERE id = COALESCE(NEW.document_id, OLD.document_id);
  IF st IS NOT NULL AND st <> 'draft' THEN
    RAISE EXCEPTION 'lines of a non-draft document cannot change' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF TG_OP = 'UPDATE' AND NEW.document_id <> OLD.document_id THEN
    RAISE EXCEPTION 'a line cannot move to another document' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER ccb_line_guard BEFORE INSERT OR UPDATE OR DELETE ON billing_lineitem
  FOR EACH ROW EXECUTE FUNCTION ccb_line_guard();

CREATE OR REPLACE FUNCTION ccb_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION '% is append-only (% refused)', TG_TABLE_NAME, TG_OP USING ERRCODE = 'integrity_constraint_violation';
END $$;

CREATE TRIGGER ccb_audit_append_only BEFORE UPDATE OR DELETE ON billing_auditevent
  FOR EACH ROW EXECUTE FUNCTION ccb_append_only();
CREATE TRIGGER ccb_payment_append_only BEFORE UPDATE OR DELETE ON billing_payment
  FOR EACH ROW EXECUTE FUNCTION ccb_append_only();
CREATE TRIGGER ccb_credit_append_only BEFORE UPDATE OR DELETE ON billing_customercredit
  FOR EACH ROW EXECUTE FUNCTION ccb_append_only();

CREATE OR REPLACE FUNCTION ccb_refund_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'refunds cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF ROW(NEW.payment_id, NEW.amount_cents, NEW.stripe_refund_id, NEW.recorded_by, NEW.created_at)
     IS DISTINCT FROM ROW(OLD.payment_id, OLD.amount_cents, OLD.stripe_refund_id, OLD.recorded_by, OLD.created_at) THEN
    RAISE EXCEPTION 'refund details cannot change' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.resolution <> 'pending' AND NEW.resolution IS DISTINCT FROM OLD.resolution THEN
    RAISE EXCEPTION 'refund % was already decided', OLD.id USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF OLD.credit_note_id IS NOT NULL AND NEW.credit_note_id IS DISTINCT FROM OLD.credit_note_id THEN
    RAISE EXCEPTION 'refund % already has a credit note', OLD.id USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER ccb_refund_guard BEFORE UPDATE OR DELETE ON billing_refund
  FOR EACH ROW EXECUTE FUNCTION ccb_refund_guard();

CREATE OR REPLACE FUNCTION ccb_stripe_event_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'stripe events cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF ROW(NEW.event_id, NEW.type, NEW.payload, NEW.received_at) IS DISTINCT FROM ROW(OLD.event_id, OLD.type, OLD.payload, OLD.received_at) THEN
    RAISE EXCEPTION 'stripe event % cannot change' , OLD.event_id USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $$;

CREATE TRIGGER ccb_stripe_event_guard BEFORE UPDATE OR DELETE ON billing_stripeevent
  FOR EACH ROW EXECUTE FUNCTION ccb_stripe_event_guard();
"""

REVERSE = """
DROP TRIGGER IF EXISTS ccb_stripe_event_guard ON billing_stripeevent;
DROP TRIGGER IF EXISTS ccb_refund_guard ON billing_refund;
DROP TRIGGER IF EXISTS ccb_credit_append_only ON billing_customercredit;
DROP TRIGGER IF EXISTS ccb_payment_append_only ON billing_payment;
DROP TRIGGER IF EXISTS ccb_audit_append_only ON billing_auditevent;
DROP TRIGGER IF EXISTS ccb_line_guard ON billing_lineitem;
DROP TRIGGER IF EXISTS ccb_document_guard ON billing_document;
DROP FUNCTION IF EXISTS ccb_stripe_event_guard();
DROP FUNCTION IF EXISTS ccb_refund_guard();
DROP FUNCTION IF EXISTS ccb_append_only();
DROP FUNCTION IF EXISTS ccb_line_guard();
DROP FUNCTION IF EXISTS ccb_document_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("billing", "0001_initial")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
