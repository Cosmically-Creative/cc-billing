"""Append-only change history with a hash chain.

Each entry stores who, when, what, before and after. Its hash covers the previous entry's hash,
so deleting or editing any row (even by someone with database access) breaks `verify_chain()`.
The database also refuses UPDATE/DELETE on the table (migration 0002); at deploy time the app's
database role additionally loses UPDATE/DELETE/TRUNCATE rights on it.
"""

import datetime as dt
import hashlib
import json

from django.core.serializers.json import DjangoJSONEncoder
from django.db import connection, transaction
from django.utils import timezone

from .models import AuditEvent

GENESIS = "0" * 64
_LOCK_KEY = 0x0CCB_A0D1  # advisory lock that serialises appends to the chain


def _jsonable(value):
    if value is None:
        return None
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def _digest(prev_hash, at, actor, actor_ref, action, entity, entity_id, before, after, note):
    body = json.dumps(
        {
            "prev": prev_hash,
            "at": at.astimezone(dt.UTC).isoformat(),
            "actor": actor,
            "actor_ref": actor_ref,
            "action": action,
            "entity": entity,
            "entity_id": str(entity_id),
            "before": before,
            "after": after,
            "note": note,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode()).hexdigest()


def record(actor, action, entity, entity_id, before=None, after=None, note="", at=None):
    at = at or timezone.now()
    before, after = _jsonable(before), _jsonable(after)
    with transaction.atomic():
        with connection.cursor() as cur:
            cur.execute("SELECT pg_advisory_xact_lock(%s)", [_LOCK_KEY])
        prev = AuditEvent.objects.order_by("-id").values_list("hash", flat=True).first() or GENESIS
        digest = _digest(prev, at, actor.kind, actor.ref, action, entity, entity_id, before, after, note)
        return AuditEvent.objects.create(
            at=at,
            actor=actor.kind,
            actor_ref=actor.ref,
            action=action,
            entity=entity,
            entity_id=str(entity_id),
            before=before,
            after=after,
            note=note,
            prev_hash=prev,
            hash=digest,
        )


def verify_chain() -> tuple[bool, str]:
    prev = GENESIS
    count = 0
    for ev in AuditEvent.objects.order_by("id").iterator():
        if ev.prev_hash != prev:
            return False, f"entry {ev.id}: link to previous entry is broken"
        expected = _digest(prev, ev.at, ev.actor, ev.actor_ref, ev.action, ev.entity, ev.entity_id, ev.before, ev.after, ev.note)
        if expected != ev.hash:
            return False, f"entry {ev.id}: contents don't match its hash"
        prev = ev.hash
        count += 1
    return True, f"{count} entries verified"


def history(entity, entity_id):
    return AuditEvent.objects.filter(entity=entity, entity_id=str(entity_id)).order_by("id")
