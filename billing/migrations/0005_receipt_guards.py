"""Receipts and PAID copies keep their facts: only delivery status and the one-time PDF/superseded
fields may change, and neither can be deleted."""

from django.db import migrations

FORWARD = """
CREATE OR REPLACE FUNCTION ccb_paid_copy_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'PAID copies cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF ROW(NEW.invoice_id, NEW.paid_in_full_on, NEW.applied, NEW.created_at)
     IS DISTINCT FROM ROW(OLD.invoice_id, OLD.paid_in_full_on, OLD.applied, OLD.created_at)
     OR (OLD.pdf_file <> '' AND ROW(NEW.pdf_file, NEW.pdf_sha256) IS DISTINCT FROM ROW(OLD.pdf_file, OLD.pdf_sha256))
     OR (OLD.superseded_at IS NOT NULL AND ROW(NEW.superseded_at, NEW.superseded_reason) IS DISTINCT FROM ROW(OLD.superseded_at, OLD.superseded_reason)) THEN
    RAISE EXCEPTION 'PAID copy % cannot change', OLD.id USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ccb_paid_copy_guard BEFORE UPDATE OR DELETE ON billing_paidcopy
  FOR EACH ROW EXECUTE FUNCTION ccb_paid_copy_guard();

CREATE OR REPLACE FUNCTION ccb_receipt_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'receipts cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF ROW(NEW.payment_id, NEW.kind, NEW.paid_copy_id, NEW.paid_so_far_cents, NEW.balance_after_cents, NEW.credit_cents, NEW.created_at)
     IS DISTINCT FROM ROW(OLD.payment_id, OLD.kind, OLD.paid_copy_id, OLD.paid_so_far_cents, OLD.balance_after_cents, OLD.credit_cents, OLD.created_at) THEN
    RAISE EXCEPTION 'receipt % facts cannot change', OLD.id USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ccb_receipt_guard BEFORE UPDATE OR DELETE ON billing_receipt
  FOR EACH ROW EXECUTE FUNCTION ccb_receipt_guard();
"""

REVERSE = """
DROP TRIGGER IF EXISTS ccb_receipt_guard ON billing_receipt;
DROP TRIGGER IF EXISTS ccb_paid_copy_guard ON billing_paidcopy;
DROP FUNCTION IF EXISTS ccb_receipt_guard();
DROP FUNCTION IF EXISTS ccb_paid_copy_guard();
"""


class Migration(migrations.Migration):
    dependencies = [("billing", "0004_receipts")]
    operations = [migrations.RunSQL(FORWARD, REVERSE)]
