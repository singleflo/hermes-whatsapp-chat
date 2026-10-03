"""Contacts: LID -> phone resolution, system/self chat filter, contact names, LID conversation repair.

WhatsApp addresses some chats by an opaque LID (``<digits>@lid``). The account's Baileys session stores the
LID -> phone mapping in ``lid-mapping-<lid>_reverse.json`` files (a bare JSON string with the phone digits).
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path
from typing import Any

from . import accounts, db, events

SYSTEM_JIDS = frozenset({"0@s.whatsapp.net", "status@broadcast"})
_IGNORED_SUFFIXES = ("@broadcast", "@newsletter", "@g.us")
_PHONE_SUFFIX = "@s.whatsapp.net"
_DIGITS = re.compile(r"[0-9]+")


def own_number(account: Any) -> str | None:
    """The account's own phone number (digits), from ``phone`` or the number part of ``wa_jid``."""
    if account["phone"]:
        return str(account["phone"])
    jid = account["wa_jid"]
    if jid:
        number = str(jid).split("@")[0].split(":")[0]
        return number or None
    return None


def is_ignored_jid(jid: str, own: str | None) -> bool:
    """System, broadcast, newsletter, group chats and the account's own "Message yourself" chat."""
    return (
        jid in SYSTEM_JIDS
        or jid.endswith(_IGNORED_SUFFIXES)
        or (own is not None and jid == f"{own}{_PHONE_SUFFIX}")
    )


def lid_to_phone(session_dir: Path, jid: str) -> str | None:
    """Phone digits for a LID JID from the session's reverse mapping file, or ``None`` when unknown."""
    user = jid.split("@", 1)[0].split(":", 1)[0]
    if not _DIGITS.fullmatch(user):
        return None
    try:
        raw = json.loads((session_dir / f"lid-mapping-{user}_reverse.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return raw if isinstance(raw, str) and _DIGITS.fullmatch(raw) else None


def resolve_jid(account: Any, jid: str) -> str:
    """Phone JID for a mapped LID JID; every other JID (and unmapped LIDs) unchanged."""
    if not jid.endswith("@lid") or not account["session_dir"]:
        return jid
    phone = lid_to_phone(accounts.session_path(account), jid)
    return f"{phone}{_PHONE_SUFFIX}" if phone else jid


# --- Names -------------------------------------------------------------------------


def apply_contact(conn: sqlite3.Connection, account_id: int, item: dict, now: int) -> str:
    """Name still-unnamed conversations from a bridge contact update. Never overwrites an existing name."""
    name = next((str(v).strip() for v in (item.get("name"), item.get("notify"), item.get("verifiedName")) if v), "")
    if not name or name.lstrip("+").isdigit():
        return "ignored"
    account = conn.execute(
        "SELECT id, phone, wa_jid, session_dir FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if account is None:
        return "ignored"
    candidates: set[str] = set()
    for key in ("jid", "lid", "pn"):
        value = item.get(key)
        if isinstance(value, str) and value:
            candidates.add(value)
            candidates.add(resolve_jid(account, value))
    if not candidates:
        return "ignored"
    marks = ",".join("?" * len(candidates))
    changed = False
    with db.write_txn(conn):
        rows = conn.execute(
            f"SELECT id FROM conversations WHERE account_id = ? AND chat_jid IN ({marks})"
            " AND (contact_name IS NULL OR contact_name = '')",
            (account_id, *sorted(candidates)),
        ).fetchall()
        for row in rows:
            conn.execute("UPDATE conversations SET contact_name = ?, updated_at = ? WHERE id = ?", (name, now, row["id"]))
            events.emit(
                conn,
                "conversation.updated",
                now=now,
                account_id=account_id,
                conversation_id=row["id"],
                payload={"fields": ["contact_name"]},
            )
            changed = True
    return "applied" if changed else "ignored"


# --- Repair ------------------------------------------------------------------------


def delete_conversation(conn: sqlite3.Connection, conversation_id: int) -> None:
    """Delete one conversation with its messages, logs, events and runs. The caller holds a transaction."""
    for table in ("messages", "conversation_state_log", "automation_runs", "events"):
        conn.execute(f"DELETE FROM {table} WHERE conversation_id = ?", (conversation_id,))
    conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))


def _max_nullable(a: int | None, b: int | None) -> int | None:
    values = [v for v in (a, b) if v is not None]
    return max(values) if values else None


def _merge_tags(dst_raw: str | None, src_raw: str | None) -> str:
    tags: list[str] = []
    for raw in (dst_raw, src_raw):
        parsed = db.jloads(raw, [])
        for tag in parsed if isinstance(parsed, list) else []:
            if isinstance(tag, str) and tag not in tags:
                tags.append(tag)
    return json.dumps(tags)


def _rename(conn: sqlite3.Connection, row: sqlite3.Row, target: str, now: int) -> None:
    conn.execute(
        "UPDATE messages SET remote_jid = COALESCE(remote_jid, ?) WHERE conversation_id = ?",
        (row["chat_jid"], row["id"]),
    )
    conn.execute(
        "UPDATE conversations SET chat_jid = ?, phone = ?, updated_at = ? WHERE id = ?",
        (target, target.split("@", 1)[0], now, row["id"]),
    )
    events.emit(
        conn,
        "conversation.updated",
        now=now,
        account_id=row["account_id"],
        conversation_id=row["id"],
        payload={"fields": ["chat_jid", "phone"]},
    )


def _merge(conn: sqlite3.Connection, src: sqlite3.Row, dst: sqlite3.Row, now: int) -> None:
    conn.execute(
        "UPDATE messages SET conversation_id = ?, remote_jid = COALESCE(remote_jid, ?) WHERE conversation_id = ?",
        (dst["id"], src["chat_jid"], src["id"]),
    )
    for table in ("conversation_state_log", "events", "automation_runs"):
        conn.execute(f"UPDATE {table} SET conversation_id = ? WHERE conversation_id = ?", (dst["id"], src["id"]))
    conn.execute(
        "UPDATE conversations SET last_message_at = ?, last_inbound_at = ?, unread_count = ?, contact_name = ?,"
        " priority = ?, tags = ?, updated_at = ? WHERE id = ?",
        (
            _max_nullable(dst["last_message_at"], src["last_message_at"]),
            _max_nullable(dst["last_inbound_at"], src["last_inbound_at"]),
            dst["unread_count"] + src["unread_count"],
            dst["contact_name"] or src["contact_name"],
            max(dst["priority"], src["priority"]),
            _merge_tags(dst["tags"], src["tags"]),
            now,
            dst["id"],
        ),
    )
    conn.execute("DELETE FROM conversations WHERE id = ?", (src["id"],))
    events.emit(
        conn,
        "conversation.updated",
        now=now,
        account_id=dst["account_id"],
        conversation_id=dst["id"],
        payload={"fields": ["merged"]},
    )


def repair_lid_conversations(conn: sqlite3.Connection, account_id: int, now: int) -> int:
    """Re-key LID conversations by phone JID, merge duplicates and drop system/self chats.

    Returns the number of conversations deleted, renamed or merged. Idempotent.
    """
    account = conn.execute(
        "SELECT id, phone, wa_jid, session_dir FROM accounts WHERE id = ?", (account_id,)
    ).fetchone()
    if account is None:
        return 0
    own = own_number(account)
    own_jid = f"{own}{_PHONE_SUFFIX}" if own else ""
    candidates = conn.execute(
        "SELECT id FROM conversations WHERE account_id = ? AND (chat_jid LIKE '%@lid'"
        " OR chat_jid IN ('0@s.whatsapp.net','status@broadcast') OR chat_jid LIKE '%@broadcast'"
        " OR chat_jid LIKE '%@newsletter' OR chat_jid LIKE '%@g.us' OR chat_jid = ?) ORDER BY id",
        (account_id, own_jid),
    ).fetchall()
    done = 0
    for candidate in candidates:
        with db.write_txn(conn):
            row = conn.execute("SELECT * FROM conversations WHERE id = ?", (candidate["id"],)).fetchone()
            if row is None:
                continue
            chat_jid = row["chat_jid"]
            if is_ignored_jid(chat_jid, own):
                delete_conversation(conn, row["id"])
                done += 1
                continue
            if not chat_jid.endswith("@lid"):
                continue
            target = resolve_jid(account, chat_jid)
            if target == chat_jid:
                continue  # still unmapped
            if is_ignored_jid(target, own):
                delete_conversation(conn, row["id"])
            else:
                existing = conn.execute(
                    "SELECT * FROM conversations WHERE account_id = ? AND chat_jid = ?", (account_id, target)
                ).fetchone()
                if existing is None:
                    _rename(conn, row, target, now)
                else:
                    _merge(conn, row, existing, now)
            done += 1
    return done
