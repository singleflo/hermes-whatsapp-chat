"""WhatsApp groups as conversations: metadata from the bridge (subject, participants) and the participant view.

Groups are keyed by their ``@g.us`` chat JID. The sidecar refreshes group metadata outside any transaction and
hands the bridge's ``GET /chat/<jid>`` JSON to ``apply_metadata``; both writers here open their own transaction.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from . import db

PHONE_SUFFIX = "@s.whatsapp.net"
_DIGITS = re.compile(r"[0-9]+")


def phone_digits(jid: str | None) -> str | None:
    """Phone digits of a phone JID (``<digits>[:device]@s.whatsapp.net``), else ``None``."""
    if not jid or not jid.endswith(PHONE_SUFFIX):
        return None
    user = jid.split("@", 1)[0].split(":", 1)[0]
    return user if _DIGITS.fullmatch(user) else None


def _participant(account: Any, detail: Any) -> dict[str, Any] | None:
    from . import contacts  # lazy: contacts pulls in events -> automations -> conversations (which imports this)

    if isinstance(detail, str):
        detail = {"id": detail}
    if not isinstance(detail, dict):
        return None
    raw_id = detail.get("id") if isinstance(detail.get("id"), str) else None
    lid = detail.get("lid") if isinstance(detail.get("lid"), str) and detail.get("lid") else None
    phone_jid = detail.get("phone") if isinstance(detail.get("phone"), str) else None
    phone = phone_digits(phone_jid) or phone_digits(raw_id)
    if phone is None:
        for candidate in (lid, raw_id):
            if candidate and candidate.endswith("@lid") and account is not None:
                resolved = contacts.resolve_jid(account, candidate)
                phone = phone_digits(resolved)
                if phone:
                    break
    jid = f"{phone}{PHONE_SUFFIX}" if phone else (lid or raw_id)
    if not jid:
        return None
    role = detail.get("admin")
    return {"jid": jid, "phone": phone, "admin": "superadmin" if role == "superadmin" else role == "admin"}


def apply_metadata(conn: sqlite3.Connection, conversation_id: int, data: dict[str, Any], now: int) -> bool:
    """Store a group's subject and participants from the bridge ``GET /chat/<jid>`` answer.

    Always bumps ``group_refreshed_at``. Returns True when the name or participants changed.
    """
    from . import events  # lazy, see _participant

    with db.write_txn(conn):
        row = conn.execute(
            "SELECT c.*, a.session_dir AS a_session_dir, a.id AS a_id FROM conversations c"
            " LEFT JOIN accounts a ON a.id = c.account_id WHERE c.id = ? AND c.is_group = 1",
            (conversation_id,),
        ).fetchone()
        if row is None:
            return False
        account = {"id": row["a_id"], "session_dir": row["a_session_dir"]}
        details: Any = data.get("participantDetails")
        if not isinstance(details, list):
            details = data.get("participants")
        if not isinstance(details, list):
            details = []
        participants: list[dict[str, Any]] = []
        seen: set[str] = set()
        for detail in details:
            item = _participant(account if account["session_dir"] else None, detail)
            if item is not None and item["jid"] not in seen:
                seen.add(item["jid"])
                participants.append(item)
        subject = data.get("name")
        subject = subject.strip() if isinstance(subject, str) else ""
        # The bridge answers with the id's number as the name when it could not read the metadata.
        if subject == row["chat_jid"].split("@", 1)[0]:
            subject = ""
        name_changed = bool(subject) and subject != row["contact_name"]
        # Without participants in the answer (a broken lookup) keep what we have.
        people_changed = bool(participants) and json.dumps(participants) != (row["participants"] or "")
        sets: list[str] = ["group_refreshed_at = ?"]
        args: list[Any] = [now]
        if name_changed:
            sets.append("contact_name = ?")
            args.append(subject)
        if people_changed:
            sets.append("participants = ?")
            args.append(json.dumps(participants))
        changed = name_changed or people_changed
        if changed:
            sets.append("updated_at = ?")
            args.append(now)
        conn.execute(f"UPDATE conversations SET {', '.join(sets)} WHERE id = ?", (*args, conversation_id))
        if changed:
            events.emit(
                conn,
                "conversation.updated",
                now=now,
                account_id=row["account_id"],
                conversation_id=conversation_id,
                payload={"fields": ["participants", "contact_name"]},
            )
    return changed


def mark_refresh_failed(conn: sqlite3.Connection, conversation_id: int, now: int) -> None:
    """A group whose metadata could not be read is retried after the refresh interval, not on every tick."""
    with db.write_txn(conn):
        conn.execute("UPDATE conversations SET group_refreshed_at = ? WHERE id = ?", (now, conversation_id))


def participant_view(conn: sqlite3.Connection, conv: Any) -> list[dict[str, Any]]:
    """Participants with a display ``name``: the latest sender name seen in this group, else the name of a direct
    conversation of the same number with that phone, else ``None``."""
    stored = db.jloads(conv["participants"], [])
    if not isinstance(stored, list) or not stored:
        return []
    seen_names: dict[str, str] = {}
    for row in conn.execute(
        "SELECT sender_jid, sender_name FROM messages WHERE conversation_id = ? AND sender_jid IS NOT NULL"
        " AND sender_name IS NOT NULL AND sender_name != '' ORDER BY ts, id",
        (conv["id"],),
    ):
        seen_names[row["sender_jid"]] = row["sender_name"]
    direct: dict[str, str] = {}
    for row in conn.execute(
        "SELECT chat_jid, phone, contact_name FROM conversations WHERE account_id = ? AND is_group = 0"
        " AND contact_name IS NOT NULL AND contact_name != ''",
        (conv["account_id"],),
    ):
        digits = row["phone"] or phone_digits(row["chat_jid"])
        if digits:
            direct[str(digits)] = row["contact_name"]
    out = []
    for item in stored:
        if not isinstance(item, dict) or not item.get("jid"):
            continue
        phone = item.get("phone")
        name = seen_names.get(item["jid"]) or (direct.get(str(phone)) if phone else None)
        out.append({"jid": item["jid"], "phone": phone, "admin": item.get("admin", False), "name": name})
    return out
