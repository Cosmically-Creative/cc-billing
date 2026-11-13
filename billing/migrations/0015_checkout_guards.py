"""Reservations keep their facts and once-bound Stripe identifiers."""
from django.db import migrations

FORWARD="""
CREATE OR REPLACE FUNCTION ccb_checkout_guard() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    RAISE EXCEPTION 'checkout attempts cannot be deleted' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  IF ROW(NEW.invoice_id, NEW.request_id, NEW.amount_cents, NEW.integration_identifier, NEW.created_at, NEW.expires_at)
     IS DISTINCT FROM ROW(OLD.invoice_id, OLD.request_id, OLD.amount_cents, OLD.integration_identifier, OLD.created_at, OLD.expires_at)
     OR (OLD.session_id IS NOT NULL AND NEW.session_id IS DISTINCT FROM OLD.session_id)
     OR (OLD.payment_intent_id IS NOT NULL AND NEW.payment_intent_id IS DISTINCT FROM OLD.payment_intent_id)
     OR (OLD.status = 'paid' AND NEW.status <> 'paid')
     OR (OLD.status IN ('failed','expired') AND NEW.status IN ('creating','open','processing')) THEN
    RAISE EXCEPTION 'checkout attempt facts or terminal status cannot change' USING ERRCODE = 'integrity_constraint_violation';
  END IF;
  RETURN NEW;
END $$;
CREATE TRIGGER ccb_checkout_guard BEFORE UPDATE OR DELETE ON billing_checkoutattempt
  FOR EACH ROW EXECUTE FUNCTION ccb_checkout_guard();
"""
REVERSE="""
DROP TRIGGER IF EXISTS ccb_checkout_guard ON billing_checkoutattempt;
DROP FUNCTION IF EXISTS ccb_checkout_guard();
"""
class Migration(migrations.Migration):
    dependencies=[('billing','0014_checkout_reservations')]
    operations=[migrations.RunSQL(FORWARD,REVERSE)]
