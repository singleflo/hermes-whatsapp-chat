"""Event log: every core mutation emits one row here, inside its own transaction.

``emit`` never opens or closes a transaction: the caller wraps it in ``db.write_txn``.
Triggerable events also enqueue matching automation runs (see ``automations``).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from . import automations

log = logging.getLogger("hermes_whatsapp_chat.events")


def emit(
    conn,
    type: str,
    *,
    now: int,
    account_id: int | None = None,
    conversation_id: int | None = None,
    message_id: int | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    """Insert an event row and enqueue automation runs for triggerable events."""
    raw = None if payload is None else json.dumps(payload, ensure_ascii=False, default=str)
    cur = conn.execute(
        "INSERT INTO events (type, account_id, conversation_id, message_id, payload, at) VALUES (?,?,?,?,?,?)",
        (type, account_id, conversation_id, message_id, raw, now),
    )
    event_id = int(cur.lastrowid or 0)
    if type in automations.TRIGGER_TYPES:
        try:
            automations.enqueue_for_event(
                conn,
                event_id=event_id,
                type=type,
                now=now,
                conversation_id=conversation_id,
                message_id=message_id,
                payload=payload or {},
            )
        except Exception:
            # A broken rule must never roll back the message/state change that raised the event.
            log.exception("automation matching failed for event %s (%s)", event_id, type)
    return event_id
