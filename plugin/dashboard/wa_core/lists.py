"""Contact lists and silence.

A list (Admin, Work, Personal, To classify, Ignored) belongs to the person or group, i.e. to its chat JID
(``contact_lists``), so it is shared by every WhatsApp number; one conversation (one number) may override it
(``conversations.list_override``). Silence (``conversations.silenced_until``) only means "no notifications".
"""

from __future__ import annotations

import sqlite3
import time
from typing import Any

from . import db, errors

LISTS = ("admin", "work", "personal", "unclassified", "ignored")
LIST_LABELS = {
    "admin": "Admin",
    "work": "Work",
    "personal": "Personal",
    "unclassified": "To classify",
    "ignored": "Ignored",
}
DEFAULT_LIST = "unclassified"
SILENCE_FOREVER = 32503680000  # 3000-01-01
MAX_SILENCE_HOURS = 8760
SCOPES = ("contact", "number")

# SQL fragments: the effective list of conversation alias ``c``.
JOIN_SQL = "LEFT JOIN contact_lists cl ON cl.jid = c.chat_jid"
EFFECTIVE_SQL = f"COALESCE(c.list_override, cl.list, '{DEFAULT_LIST}')"


def catalog() -> list[dict[str, str]]:
    return [{"id": name, "label": LIST_LABELS[name]} for name in LISTS]


def clean_list(value: Any) -> str:
    if value not in LISTS:
        raise errors.Invalid(f"list must be one of {list(LISTS)}")
    return value


def is_silenced(silenced_until: int | None, now: int) -> bool:
    return silenced_until is not None and silenced_until > now


def _emit_updated(conn: sqlite3.Connection, row: sqlite3.Row, fields: list[str], now: int, **extra: Any) -> None:
    from . import events  # lazy: events pulls in the automations slice, which imports this module

    events.emit(
        conn,
        "conversation.updated",
        now=now,
        account_id=row["account_id"],
        conversation_id=row["id"],
        payload={"fields": fields, **extra},
    )


def _row(conn: sqlite3.Connection, conversation_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise errors.NotFound(f"conversation {conversation_id} not found")
    return row


# --- Defaults for new conversations ------------------------------------------------


def ensure_contact_list(
    conn: sqlite3.Connection, jid: str, account_row: Any, *, is_group: bool, now: int | None = None
) -> str:
    """Give a JID without a list the number's default for new contacts / new groups. Caller's transaction.

    Returns the JID's list (existing or just inserted).
    """
    found = conn.execute("SELECT list FROM contact_lists WHERE jid = ?", (jid,)).fetchone()
    if found is not None:
        return found["list"]
    column = "new_group_list" if is_group else "new_contact_list"
    account = conn.execute(f"SELECT {column} FROM accounts WHERE id = ?", (account_row["id"],)).fetchone()
    chosen = account[0] if account is not None and account[0] in LISTS else DEFAULT_LIST
    conn.execute(
        "INSERT OR IGNORE INTO contact_lists (jid, list, updated_at) VALUES (?,?,?)",
        (jid, chosen, now if now is not None else int(time.time())),
    )
    return chosen


def effective(conn: sqlite3.Connection, conversation_id: int) -> str:
    row = conn.execute(
        f"SELECT {EFFECTIVE_SQL} FROM conversations c {JOIN_SQL} WHERE c.id = ?", (conversation_id,)
    ).fetchone()
    return row[0] if row is not None else DEFAULT_LIST


def event_flags(conn: sqlite3.Connection, conversation_id: int, now: int) -> dict[str, Any]:
    """``is_group`` / ``list`` / ``silenced`` for event payloads, so UIs decide notifications without a fetch."""
    row = conn.execute(
        f"SELECT c.is_group, c.silenced_until, {EFFECTIVE_SQL} AS eff FROM conversations c {JOIN_SQL} WHERE c.id = ?",
        (conversation_id,),
    ).fetchone()
    if row is None:
        return {"is_group": False, "list": DEFAULT_LIST, "silenced": False}
    return {
        "is_group": bool(row["is_group"]),
        "list": row["eff"],
        "silenced": is_silenced(row["silenced_until"], now),
    }


# --- Mutations ---------------------------------------------------------------------


def set_list(
    conn: sqlite3.Connection,
    conversation_id: int,
    list_id: str | None,
    scope: str = "contact",
    *,
    now: int,
    actor: str = "user",
) -> str:
    """Move a conversation's contact (every number) or just this number into a list. Returns the effective list.

    ``contact``: upsert ``contact_lists[chat_jid]`` and clear this conversation's override.
    ``number``: set ``list_override``; ``None`` clears it (follow the contact).
    """
    if scope not in SCOPES:
        raise errors.Invalid(f"scope must be one of {list(SCOPES)}")
    if list_id is None:
        if scope != "number":
            raise errors.Invalid("list may be null only with scope 'number'")
    else:
        clean_list(list_id)
    with db.write_txn(conn):
        row = _row(conn, conversation_id)
        if scope == "number":
            conn.execute(
                "UPDATE conversations SET list_override = ?, updated_at = ? WHERE id = ?", (list_id, now, row["id"])
            )
            _emit_updated(conn, row, ["list"], now, actor=actor)
        else:
            conn.execute(
                "INSERT INTO contact_lists (jid, list, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(jid) DO UPDATE SET list = excluded.list, updated_at = excluded.updated_at",
                (row["chat_jid"], list_id, now),
            )
            conn.execute(
                "UPDATE conversations SET list_override = NULL, updated_at = ? WHERE id = ?", (now, row["id"])
            )
            for other in conn.execute("SELECT * FROM conversations WHERE chat_jid = ? ORDER BY id", (row["chat_jid"],)):
                _emit_updated(conn, other, ["list"], now, actor=actor)
        return effective(conn, conversation_id)


def silence(conn: sqlite3.Connection, conversation_id: int, hours: int | None, *, now: int) -> int:
    """No notifications for ``hours`` (1..8760) or forever (``None``). Returns ``silenced_until``."""
    if hours is None:
        until = SILENCE_FOREVER
    elif isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= MAX_SILENCE_HOURS:
        raise errors.Invalid(f"hours must be between 1 and {MAX_SILENCE_HOURS}")
    else:
        until = now + hours * 3600
    with db.write_txn(conn):
        row = _row(conn, conversation_id)
        conn.execute("UPDATE conversations SET silenced_until = ?, updated_at = ? WHERE id = ?", (until, now, row["id"]))
        _emit_updated(conn, row, ["silenced_until"], now)
    return until


def unsilence(conn: sqlite3.Connection, conversation_id: int, *, now: int) -> None:
    with db.write_txn(conn):
        row = _row(conn, conversation_id)
        if row["silenced_until"] is not None:
            conn.execute(
                "UPDATE conversations SET silenced_until = NULL, updated_at = ? WHERE id = ?", (now, row["id"])
            )
            _emit_updated(conn, row, ["silenced_until"], now)
