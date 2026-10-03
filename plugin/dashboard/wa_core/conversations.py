"""Conversations: board/list/detail/messages/search, guarded transitions, tags, timers."""

from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable

from . import accounts, db, errors, events, media, settings

# Single source of column order. Keep exhaustive + fallback: a state missing here must
# land in the fallback column, never be dropped.
BOARD_COLUMNS: list[str] = ["new", "in_progress", "waiting", "muted", "closed"]
FALLBACK_COLUMN = "in_progress"
CARD_PREVIEW_CHARS = 200
PENDING_TIMEOUT_SECONDS = 180
MAX_TAGS = 20
MAX_TAG_CHARS = 40

ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "new": {"in_progress", "muted", "closed"},
    "in_progress": {"waiting", "muted", "closed"},
    "waiting": {"in_progress", "muted", "closed"},
    "muted": {"in_progress", "new"},
    "closed": {"in_progress"},
}

_HIDDEN_STATUSES = "('draft','discarded')"
_LAST_ID = (
    "(SELECT m.id FROM messages m WHERE m.conversation_id = c.id"
    f" AND m.status NOT IN {_HIDDEN_STATUSES} ORDER BY m.ts DESC, m.id DESC LIMIT 1)"
)
_CARD_SELECT = (
    "SELECT c.*, a.label AS account_label, a.color AS account_color,"
    " lm.body AS lm_body, lm.direction AS lm_direction, lm.author AS lm_author, lm.meta AS lm_meta,"
    " lm.status AS lm_status, lm.source AS lm_source,"
    " EXISTS(SELECT 1 FROM messages d WHERE d.conversation_id = c.id AND d.status = 'draft') AS has_draft"
    " FROM conversations c LEFT JOIN accounts a ON a.id = c.account_id"
    f" LEFT JOIN messages lm ON lm.id = {_LAST_ID}"
)


# --- Helpers -------------------------------------------------------------------


def tags_of(raw: str | None) -> list[str]:
    tags = db.jloads(raw, [])
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []


def like_pattern(q: str) -> str:
    escaped = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def allowed_from(state: str) -> set[str]:
    return ALLOWED_TRANSITIONS.get(state, ALLOWED_TRANSITIONS[FALLBACK_COLUMN])


def get_row(conn: sqlite3.Connection, conversation_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise errors.NotFound(f"conversation {conversation_id} not found")
    return row


def _preview(row: sqlite3.Row) -> str:
    body = row["lm_body"] or ""
    if not body.strip():
        meta = db.jloads(row["lm_meta"], None)
        media_type = meta.get("mediaType") if isinstance(meta, dict) else None
        body = f"[{media_type}]" if media_type else ""
    return body[:CARD_PREVIEW_CHARS]


def _card(row: sqlite3.Row, now: int, urgency_hours: int) -> dict[str, Any]:
    tags = tags_of(row["tags"])
    last_at = row["last_message_at"]
    age = now - last_at if last_at is not None else None
    if row["priority"] >= 2 or "urgent" in tags:
        urgency = "high"
    elif age is not None and age > urgency_hours * 3600 and row["state"] in ("new", "waiting"):
        urgency = "medium"
    else:
        urgency = "normal"
    return {
        "id": row["id"],
        "account_id": row["account_id"],
        "account_label": row["account_label"] if row["account_label"] is not None else "Removed account",
        "account_color": row["account_color"] if row["account_color"] is not None else "gray",
        "chat_jid": row["chat_jid"],
        "contact_name": row["contact_name"],
        "phone": row["phone"],
        "state": row["state"],
        "priority": row["priority"],
        "last_message_preview": _preview(row),
        "last_message_direction": row["lm_direction"],
        "last_message_author": row["lm_author"],
        "last_message_status": row["lm_status"] if row["lm_direction"] == "out" and row["lm_source"] == "live" else None,
        "last_message_at": last_at,
        "age_seconds": age,
        "unread_count": row["unread_count"],
        "agent_active": bool(row["agent_active"]),
        "muted_until": row["muted_until"],
        "tags": tags,
        "urgency": urgency,
        "has_draft": bool(row["has_draft"]),
    }


def message_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "conversation_id": row["conversation_id"],
        "direction": row["direction"],
        "author": row["author"],
        "body": row["body"],
        "ts": row["ts"],
        "status": row["status"],
        "delivered_at": row["delivered_at"],
        "read_at": row["read_at"],
        "source": row["source"],
        "wa_id": row["wa_id"],
        "error": row["error"],
        "rule_id": row["rule_id"],
        "media": media.message_media(row["meta"]),
    }


def get_message(conn: sqlite3.Connection, message_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise errors.NotFound(f"message {message_id} not found")
    return message_dict(row)


def _filters(account_id: int | None, q: str | None, extra: str = "", args: list | None = None) -> tuple[str, list]:
    clauses, params = ([extra] if extra else []), list(args or [])
    if account_id is not None:
        clauses.append("c.account_id = ?")
        params.append(account_id)
    if q and q.strip():
        clauses.append(
            "(c.contact_name LIKE ? ESCAPE '\\' OR c.phone LIKE ? ESCAPE '\\' OR c.chat_jid LIKE ? ESCAPE '\\')"
        )
        params += [like_pattern(q)] * 3
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def summary(conn: sqlite3.Connection, conversation_id: int) -> dict[str, Any]:
    row = get_row(conn, conversation_id)
    return {
        "ok": True,
        "id": row["id"],
        "state": row["state"],
        "agent_active": bool(row["agent_active"]),
        "priority": row["priority"],
        "muted_until": row["muted_until"],
        "unread_count": row["unread_count"],
        "tags": tags_of(row["tags"]),
    }


# --- Reads ---------------------------------------------------------------------


def get_card(conn: sqlite3.Connection, conversation_id: int, now: int) -> dict[str, Any]:
    row = conn.execute(_CARD_SELECT + " WHERE c.id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise errors.NotFound(f"conversation {conversation_id} not found")
    return _card(row, now, settings.get_settings(conn).board.urgency_hours)


def get_board(
    conn: sqlite3.Connection, *, account_id: int | None = None, q: str = "", include_closed: bool = False, now: int
) -> dict[str, Any]:
    """Columns + cards. Two queries at most; the last message is one correlated subquery per row."""
    run_timers(conn, now)
    cfg = settings.get_settings(conn)
    by_col: dict[str, list[dict]] = {name: [] for name in BOARD_COLUMNS}
    where, args = _filters(account_id, q, "c.state != 'closed'")
    rows = conn.execute(_CARD_SELECT + where + " ORDER BY c.priority DESC, c.last_message_at ASC, c.id", args).fetchall()
    for row in rows:
        column = row["state"] if row["state"] in by_col else FALLBACK_COLUMN
        by_col[column].append(_card(row, now, cfg.board.urgency_hours))
    if include_closed:
        where, args = _filters(account_id, q, "c.state = 'closed'")
        rows = conn.execute(
            _CARD_SELECT + where + " ORDER BY c.last_message_at DESC, c.id DESC LIMIT ?", (*args, cfg.board.closed_limit)
        ).fetchall()
        by_col["closed"] = [_card(row, now, cfg.board.urgency_hours) for row in rows]
    where, args = _filters(account_id, q)
    counts = {name: 0 for name in BOARD_COLUMNS}
    sql = "SELECT c.state AS state, COUNT(*) AS n FROM conversations c" + where + " GROUP BY c.state"
    for row in conn.execute(sql, args):
        counts[row["state"]] = row["n"]
    return {"columns": [{"name": name, "cards": by_col[name]} for name in BOARD_COLUMNS], "counts": counts, "now": now}


def list_conversations(
    conn: sqlite3.Connection,
    *,
    account_id: int | None = None,
    state: str | None = None,
    q: str = "",
    unread_only: bool = False,
    limit: int = 50,
    offset: int = 0,
    now: int,
) -> dict[str, Any]:
    run_timers(conn, now)
    extra, args = [], []
    if state:
        extra.append("c.state = ?")
        args.append(state)
    if unread_only:
        extra.append("c.unread_count > 0")
    where, params = _filters(account_id, q, " AND ".join(extra), args)
    total = conn.execute("SELECT COUNT(*) FROM conversations c" + where, params).fetchone()[0]
    rows = conn.execute(
        _CARD_SELECT + where + " ORDER BY c.last_message_at DESC, c.id DESC LIMIT ? OFFSET ?",
        (*params, max(1, min(limit, 500)), max(0, offset)),
    ).fetchall()
    urgency_hours = settings.get_settings(conn).board.urgency_hours
    return {"conversations": [_card(r, now, urgency_hours) for r in rows], "total": total, "now": now}


def get_conversation(conn: sqlite3.Connection, conversation_id: int, *, now: int) -> dict[str, Any]:
    card = get_card(conn, conversation_id, now)
    row = get_row(conn, conversation_id)
    try:
        account = accounts.get_account(conn, row["account_id"])
    except errors.NotFound:
        account = None
    history = conn.execute(
        "SELECT * FROM conversation_state_log WHERE conversation_id = ? ORDER BY id DESC LIMIT 50", (conversation_id,)
    ).fetchall()
    runs = conn.execute(
        "SELECT r.*, ru.name AS rule_name FROM automation_runs r LEFT JOIN automation_rules ru ON ru.id = r.rule_id"
        " WHERE r.conversation_id = ? ORDER BY r.id DESC LIMIT 20",
        (conversation_id,),
    ).fetchall()
    return {
        "conversation": {**card, "previous_state": row["previous_state"], "created_at": row["created_at"]},
        "account": account,
        "allowed_states": sorted(allowed_from(row["state"])),
        "state_history": [dict(h) for h in history],
        "runs": [dict(r) for r in runs],
    }


def list_messages(
    conn: sqlite3.Connection, conversation_id: int, *, before_id: int | None = None, limit: int = 50
) -> dict[str, Any]:
    get_row(conn, conversation_id)
    limit = max(1, min(limit, 200))
    where, args = "m.conversation_id = ? AND m.status != 'discarded'", [conversation_id]
    if before_id is not None:
        anchor = conn.execute("SELECT ts FROM messages WHERE id = ?", (before_id,)).fetchone()
        if anchor is None:
            where += " AND m.id < ?"
            args.append(before_id)
        else:
            where += " AND (m.ts < ? OR (m.ts = ? AND m.id < ?))"
            args += [anchor["ts"], anchor["ts"], before_id]
    rows = conn.execute(
        f"SELECT m.* FROM messages m WHERE {where} ORDER BY m.ts DESC, m.id DESC LIMIT ?", (*args, limit + 1)
    ).fetchall()
    has_more = len(rows) > limit
    return {"messages": [message_dict(r) for r in reversed(rows[:limit])], "has_more": has_more}


def search_messages(
    conn: sqlite3.Connection, q: str, *, account_id: int | None = None, now: int
) -> dict[str, Any]:
    if not q or not q.strip():
        return {"results": []}
    where = f"m.body LIKE ? ESCAPE '\\' AND m.status NOT IN {_HIDDEN_STATUSES}"
    args: list[Any] = [like_pattern(q)]
    if account_id is not None:
        where += " AND m.account_id = ?"
        args.append(account_id)
    rows = conn.execute(
        f"SELECT m.* FROM messages m WHERE {where} ORDER BY m.ts DESC, m.id DESC LIMIT 50", args
    ).fetchall()
    if not rows:
        return {"results": []}
    urgency_hours = settings.get_settings(conn).board.urgency_hours
    ids = sorted({r["conversation_id"] for r in rows})
    cards = {
        r["id"]: _card(r, now, urgency_hours)
        for r in conn.execute(_CARD_SELECT + f" WHERE c.id IN ({','.join('?' * len(ids))})", ids)
    }
    return {
        "results": [
            {"message": message_dict(r), "conversation": cards[r["conversation_id"]]}
            for r in rows
            if r["conversation_id"] in cards
        ]
    }


# --- Transitions -----------------------------------------------------------------


def force_transition(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    target: str,
    *,
    actor: str,
    reason: str | None,
    now: int,
    muted_until: int | None = None,
) -> None:
    """Move without consulting the transition table (policy moves). Caller holds a write txn."""
    current = row["state"]
    conn.execute(
        "UPDATE conversations SET state = ?, previous_state = ?, muted_until = ?, updated_at = ? WHERE id = ?",
        (target, current, muted_until if target == "muted" else None, now, row["id"]),
    )
    conn.execute(
        "INSERT INTO conversation_state_log (conversation_id, from_state, to_state, actor, reason, at)"
        " VALUES (?,?,?,?,?,?)",
        (row["id"], current, target, actor, reason, now),
    )
    events.emit(
        conn,
        "conversation.state_changed",
        now=now,
        account_id=row["account_id"],
        conversation_id=row["id"],
        payload={"from": current, "to": target, "actor": actor, "reason": reason},
    )


def _emit_updated(conn: sqlite3.Connection, row: sqlite3.Row, fields: list[str], now: int) -> None:
    events.emit(
        conn,
        "conversation.updated",
        now=now,
        account_id=row["account_id"],
        conversation_id=row["id"],
        payload={"fields": fields},
    )


def _guard(row: sqlite3.Row, target: str, muted_until: int | None, now: int) -> None:
    current = row["state"]
    if target not in BOARD_COLUMNS:
        raise errors.Invalid(f"unknown state: {target}")
    allowed = allowed_from(current)
    if target not in allowed:
        raise errors.Conflict(
            f"cannot move conversation {row['id']} from '{current}' to '{target}'; allowed from '{current}': {sorted(allowed)}"
        )
    if target == "muted" and (muted_until is None or muted_until <= now):
        raise errors.Invalid("muted_until must be a future unix timestamp")


def set_state(
    conn: sqlite3.Connection,
    conversation_id: int,
    state: str,
    *,
    now: int,
    actor: str = "user",
    reason: str | None = None,
    muted_until: int | None = None,
) -> dict[str, Any]:
    """Guarded transition: domain check + audit row + event in the SAME transaction."""
    with db.write_txn(conn):
        row = get_row(conn, conversation_id)
        _guard(row, state, muted_until, now)
        force_transition(conn, row, state, actor=actor, reason=reason, now=now, muted_until=muted_until)
    return summary(conn, conversation_id)


def mute(
    conn: sqlite3.Connection,
    conversation_id: int,
    muted_until: int,
    *,
    now: int,
    actor: str = "user",
    reason: str | None = None,
) -> dict[str, Any]:
    return set_state(
        conn, conversation_id, "muted", now=now, actor=actor, reason=reason or "muted", muted_until=muted_until
    )


def takeover(conn: sqlite3.Connection, conversation_id: int, *, now: int, actor: str = "user") -> dict[str, Any]:
    """A human takes the conversation: state in_progress, agent_active off."""
    with db.write_txn(conn):
        row = get_row(conn, conversation_id)
        moved = row["state"] != "in_progress"
        if moved:
            _guard(row, "in_progress", None, now)
            force_transition(conn, row, "in_progress", actor=actor, reason="takeover", now=now)
        if row["agent_active"]:
            conn.execute("UPDATE conversations SET agent_active = 0, updated_at = ? WHERE id = ?", (now, conversation_id))
            if not moved:
                _log_same_state(conn, row, "takeover", actor, now)
            _emit_updated(conn, row, ["agent_active"], now)
    return summary(conn, conversation_id)


def handback(conn: sqlite3.Connection, conversation_id: int, *, now: int, actor: str = "user") -> dict[str, Any]:
    with db.write_txn(conn):
        row = get_row(conn, conversation_id)
        if not row["agent_active"]:
            conn.execute("UPDATE conversations SET agent_active = 1, updated_at = ? WHERE id = ?", (now, conversation_id))
            _log_same_state(conn, row, "handback", actor, now)
            _emit_updated(conn, row, ["agent_active"], now)
    return summary(conn, conversation_id)


def escalate(
    conn: sqlite3.Connection, conversation_id: int, escalated: bool, *, now: int, actor: str = "user"
) -> dict[str, Any]:
    with db.write_txn(conn):
        row = get_row(conn, conversation_id)
        priority = 2 if escalated else 0
        if priority != row["priority"]:
            conn.execute("UPDATE conversations SET priority = ?, updated_at = ? WHERE id = ?", (priority, now, conversation_id))
            _log_same_state(conn, row, "escalated" if escalated else "de-escalated", actor, now)
            _emit_updated(conn, row, ["priority"], now)
    return summary(conn, conversation_id)


def mark_read(conn: sqlite3.Connection, conversation_id: int, *, now: int) -> dict[str, Any]:
    with db.write_txn(conn):
        row = get_row(conn, conversation_id)
        if row["unread_count"] > 0:
            conn.execute("UPDATE conversations SET unread_count = 0 WHERE id = ?", (conversation_id,))
            _emit_updated(conn, row, ["unread_count"], now)
    return summary(conn, conversation_id)


def _log_same_state(conn: sqlite3.Connection, row: sqlite3.Row, reason: str, actor: str, now: int) -> None:
    conn.execute(
        "INSERT INTO conversation_state_log (conversation_id, from_state, to_state, actor, reason, at)"
        " VALUES (?,?,?,?,?,?)",
        (row["id"], row["state"], row["state"], actor, reason, now),
    )


def _clean_tags(tags: Iterable[Any]) -> list[str]:
    out: list[str] = []
    for tag in tags:
        text = str(tag).strip()
        if not text:
            continue
        if len(text) > MAX_TAG_CHARS:
            raise errors.Invalid(f"tags are limited to {MAX_TAG_CHARS} characters")
        if text not in out:
            out.append(text)
    if len(out) > MAX_TAGS:
        raise errors.Invalid(f"at most {MAX_TAGS} tags per conversation")
    return out


def _write_tags(conn: sqlite3.Connection, row: sqlite3.Row, tags: list[str], now: int) -> None:
    if tags != tags_of(row["tags"]):
        conn.execute("UPDATE conversations SET tags = ?, updated_at = ? WHERE id = ?", (json.dumps(tags), now, row["id"]))
        _emit_updated(conn, row, ["tags"], now)


def set_tags(conn: sqlite3.Connection, conversation_id: int, tags: list[str], *, now: int) -> dict[str, Any]:
    clean = _clean_tags(tags)
    with db.write_txn(conn):
        _write_tags(conn, get_row(conn, conversation_id), clean, now)
    return summary(conn, conversation_id)


def update_tags(
    conn: sqlite3.Connection,
    conversation_id: int,
    *,
    add: Iterable[str] = (),
    remove: Iterable[str] = (),
    now: int,
) -> dict[str, Any]:
    removed = {str(t).strip() for t in remove}
    with db.write_txn(conn):
        row = get_row(conn, conversation_id)
        merged = _clean_tags([*tags_of(row["tags"]), *add])
        _write_tags(conn, row, [t for t in merged if t not in removed], now)
    return summary(conn, conversation_id)


def apply_outbound(conn: sqlite3.Connection, conversation_id: int, *, ts: int, now: int, reason: str) -> None:
    """Rule for an outbound message (board send, agent, or typed on the phone). Caller holds a write txn."""
    row = get_row(conn, conversation_id)
    conn.execute(
        "UPDATE conversations SET last_message_at = MAX(COALESCE(last_message_at, 0), ?), unread_count = 0,"
        " updated_at = ? WHERE id = ?",
        (ts, now, conversation_id),
    )
    if row["state"] in ("new", "in_progress") and settings.get_settings(conn).rules.outbound_on_active == "waiting":
        force_transition(conn, row, "waiting", actor="auto", reason=reason, now=now)


# --- Timers ----------------------------------------------------------------------

_MUTE_DUE = "SELECT * FROM conversations WHERE state = 'muted' AND muted_until IS NOT NULL AND muted_until <= ?"
_STALE_PENDING = "SELECT * FROM messages WHERE status = 'pending' AND ts <= ?"


def run_timers(conn: sqlite3.Connection, now: int) -> int:
    """Expire mutes, auto-close stale waiting conversations, fail abandoned sends. Returns changes."""
    days = settings.get_settings(conn).rules.auto_close_waiting_days
    stale_waiting = (
        "SELECT * FROM conversations WHERE state = 'waiting' AND last_message_at IS NOT NULL AND last_message_at < ?"
    )
    waiting_cutoff = now - (days or 0) * 86400
    pending_cutoff = now - PENDING_TIMEOUT_SECONDS
    due = (
        conn.execute(_MUTE_DUE, (now,)).fetchone()
        or (days is not None and conn.execute(stale_waiting, (waiting_cutoff,)).fetchone())
        or conn.execute(_STALE_PENDING, (pending_cutoff,)).fetchone()
    )
    if not due:
        return 0
    changes = 0
    with db.write_txn(conn):
        for row in conn.execute(_MUTE_DUE, (now,)).fetchall():
            force_transition(conn, row, "new", actor="auto", reason="mute expired", now=now)
            changes += 1
        if days is not None:
            for row in conn.execute(stale_waiting, (waiting_cutoff,)).fetchall():
                force_transition(conn, row, "closed", actor="auto", reason=f"auto-close after {days} days", now=now)
                changes += 1
        for msg in conn.execute(_STALE_PENDING, (pending_cutoff,)).fetchall():
            conn.execute(
                "UPDATE messages SET status = 'failed', error = 'send interrupted before the bridge answered'"
                " WHERE id = ?",
                (msg["id"],),
            )
            events.emit(
                conn,
                "message.status",
                now=now,
                account_id=msg["account_id"],
                conversation_id=msg["conversation_id"],
                message_id=msg["id"],
                payload={"status": "failed"},
            )
            changes += 1
    return changes
