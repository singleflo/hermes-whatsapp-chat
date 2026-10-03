"""Outbound messages: send text/media through the account's bridge, and the draft lifecycle."""

from __future__ import annotations

import json
import mimetypes
import re
import secrets
import sqlite3
from pathlib import Path
from typing import Any

from . import accounts, bridge, conversations, db, errors, events, settings

WA_JID_SUFFIXES = ("@s.whatsapp.net", "@lid")
SEND_TIMEOUT_SECONDS = 70
_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9._ -]+")

# Delivery order of an outbound message; a receipt only ever moves a message up this ladder.
STATUS_RANK = {"pending": 0, "failed": 0, "sent": 1, "delivered": 2, "read": 3, "played": 4}
# Baileys proto.WebMessageInfo.Status -> our status (ERROR 0 / PENDING 1 carry no receipt).
RECEIPT_STATUS = {2: "sent", 3: "delivered", 4: "read", 5: "played"}
READ_RECEIPT_BATCH = 200
READ_RECEIPT_TIMEOUT_SECONDS = 10


# --- Shared steps ------------------------------------------------------------------


def _prepare(conn: sqlite3.Connection, conversation_id: int) -> tuple[sqlite3.Row, dict[str, Any]]:
    conv = conversations.get_row(conn, conversation_id)
    account = accounts.get_account(conn, conv["account_id"])
    if account["kind"] == "demo":
        raise errors.Invalid("demo conversations can never be messaged")
    if not conv["chat_jid"].endswith(WA_JID_SUFFIXES):
        raise errors.Invalid(f"replies are only possible to WhatsApp chats (got {conv['chat_jid']})")
    if account["desired"] != "running" or not account["paired"]:
        raise errors.Unavailable(f"account '{account['label']}' is not paired and running")
    return conv, account


def _emit_status(conn: sqlite3.Connection, conv: sqlite3.Row, message_id: int, status: str, now: int) -> None:
    events.emit(
        conn,
        "message.status",
        now=now,
        account_id=conv["account_id"],
        conversation_id=conv["id"],
        message_id=message_id,
        payload={"status": status},
    )


def _insert_pending(
    conn: sqlite3.Connection,
    conv: sqlite3.Row,
    text: str,
    *,
    author: str,
    now: int,
    rule_id: int | None,
    meta: dict | None,
) -> int:
    with db.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, account_id, wa_id, direction, author, body, ts, status, source,"
            " meta, rule_id) VALUES (?,?,NULL,'out',?,?,?,'pending','live',?,?)",
            (conv["id"], conv["account_id"], author, text, now, json.dumps(meta) if meta else None, rule_id),
        )
        message_id = cur.lastrowid or 0
        _emit_status(conn, conv, message_id, "pending", now)
    return message_id


def _fail(conn: sqlite3.Connection, conv: sqlite3.Row, message_id: int, error: str, now: int) -> None:
    with db.write_txn(conn):
        conn.execute("UPDATE messages SET status = 'failed', error = ? WHERE id = ?", (error, message_id))
        _emit_status(conn, conv, message_id, "failed", now)


def _succeed(conn: sqlite3.Connection, conv: sqlite3.Row, message_id: int, wa_id: str | None, now: int) -> None:
    with db.write_txn(conn):
        row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
        if wa_id:
            # The bridge may have echoed our own send back before we recorded its id: keep one row.
            conn.execute(
                "DELETE FROM messages WHERE account_id = ? AND wa_id = ? AND id != ?"
                " AND direction = 'out' AND author IN ('phone','auto_reply')",
                (row["account_id"], wa_id, message_id),
            )
            if conn.execute(
                "SELECT 1 FROM messages WHERE account_id = ? AND wa_id = ? AND id != ?",
                (row["account_id"], wa_id, message_id),
            ).fetchone():
                wa_id = None
        conn.execute("UPDATE messages SET status = 'sent', wa_id = ?, error = NULL WHERE id = ?", (wa_id, message_id))
        conversations.apply_outbound(conn, conv["id"], ts=row["ts"], now=now, reason="reply sent")
        _emit_status(conn, conv, message_id, "sent", now)
        events.emit(
            conn,
            "message.out",
            now=now,
            account_id=conv["account_id"],
            conversation_id=conv["id"],
            message_id=message_id,
            payload={"author": row["author"]},
        )


def _deliver(
    conn: sqlite3.Connection,
    conv: sqlite3.Row,
    account: dict[str, Any],
    message_id: int,
    endpoint: str,
    payload: dict[str, Any],
    now: int,
) -> dict[str, Any]:
    try:
        status, data = bridge.bridge_request(account["port"], "POST", endpoint, payload, timeout=SEND_TIMEOUT_SECONDS)
    except bridge.BridgeUnavailable as exc:
        _fail(conn, conv, message_id, f"WhatsApp channel not running: {exc}", now)
        raise errors.Unavailable(f"WhatsApp channel not running: {exc}") from exc
    data = data if isinstance(data, dict) else {}
    if status != 200 or not data.get("success"):
        detail = f"WhatsApp send failed: {data.get('error') or status}"
        _fail(conn, conv, message_id, detail, now)
        raise (errors.Unavailable if status == 503 else errors.BadGateway)(detail)
    _succeed(conn, conv, message_id, data.get("messageId") or None, now)
    return conversations.get_message(conn, message_id)


# --- Send --------------------------------------------------------------------------


def send_text(
    conn: sqlite3.Connection,
    conversation_id: int,
    text: str,
    *,
    author: str,
    now: int,
    rule_id: int | None = None,
) -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        raise errors.Invalid("reply text is empty")
    conv, account = _prepare(conn, conversation_id)
    message_id = _insert_pending(conn, conv, body, author=author, now=now, rule_id=rule_id, meta=None)
    return _deliver(conn, conv, account, message_id, "/send", {"chatId": conv["chat_jid"], "message": body}, now)


def _media_type(mime: str | None) -> str:
    kind = (mime or "").split("/", 1)[0]
    return kind if kind in ("image", "video", "audio") else "document"


def _safe_filename(filename: str) -> str:
    name = _UNSAFE_NAME.sub("_", Path(filename or "").name).strip(" .")[:100]
    return name or "file"


def send_media(
    conn: sqlite3.Connection,
    conversation_id: int,
    *,
    filename: str,
    mime: str | None,
    data: bytes,
    caption: str | None,
    author: str,
    now: int,
) -> dict[str, Any]:
    if not data:
        raise errors.Invalid("media file is empty")
    max_mb = settings.get_settings(conn).media.max_upload_mb
    if len(data) > max_mb * 1024 * 1024:
        raise errors.TooLarge(f"media is larger than the {max_mb} MB upload limit")
    conv, account = _prepare(conn, conversation_id)
    name = _safe_filename(filename)
    mime = (mime or "").strip() or mimetypes.guess_type(name)[0]
    path = db.data_dir() / "uploads" / str(account["id"]) / f"{now}-{secrets.token_hex(4)}-{name}"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    text = (caption or "").strip()
    meta = {
        "mediaType": _media_type(mime),
        "media": [{"path": str(path), "mime": mime, "name": name, "size": len(data)}],
    }
    message_id = _insert_pending(conn, conv, text, author=author, now=now, rule_id=None, meta=meta)
    payload = {"chatId": conv["chat_jid"], "filePath": str(path), "caption": text, "fileName": name}
    return _deliver(conn, conv, account, message_id, "/send-media", payload, now)


# --- Drafts ------------------------------------------------------------------------


def create_draft(
    conn: sqlite3.Connection,
    conversation_id: int,
    text: str,
    *,
    author: str,
    now: int,
    rule_id: int | None = None,
) -> dict[str, Any]:
    body = (text or "").strip()
    if not body:
        raise errors.Invalid("draft text is empty")
    with db.write_txn(conn):
        conv = conversations.get_row(conn, conversation_id)
        cur = conn.execute(
            "INSERT INTO messages (conversation_id, account_id, wa_id, direction, author, body, ts, status, source,"
            " rule_id) VALUES (?,?,NULL,'out',?,?,?,'draft','live',?)",
            (conv["id"], conv["account_id"], author, body, now, rule_id),
        )
        message_id = cur.lastrowid or 0
        events.emit(
            conn,
            "message.draft",
            now=now,
            account_id=conv["account_id"],
            conversation_id=conv["id"],
            message_id=message_id,
            payload={"author": author, "rule_id": rule_id},
        )
    return conversations.get_message(conn, message_id)


def _draft_row(conn: sqlite3.Connection, message_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise errors.NotFound(f"message {message_id} not found")
    if row["status"] != "draft":
        raise errors.Conflict(f"message {message_id} is not a draft (status: {row['status']})")
    return row


def approve_draft(
    conn: sqlite3.Connection, message_id: int, *, text: str | None = None, now: int
) -> dict[str, Any]:
    """Send a draft; ``text`` replaces its body. The draft stays a draft if sending is impossible upfront."""
    row = _draft_row(conn, message_id)
    body = (row["body"] if text is None else text or "").strip()
    if not body:
        raise errors.Invalid("reply text is empty")
    conv, account = _prepare(conn, row["conversation_id"])
    with db.write_txn(conn):
        _draft_row(conn, message_id)  # re-check inside the lock: two approvals must not both send
        conn.execute("UPDATE messages SET status = 'pending', body = ?, ts = ? WHERE id = ?", (body, now, message_id))
        _emit_status(conn, conv, message_id, "pending", now)
    return _deliver(conn, conv, account, message_id, "/send", {"chatId": conv["chat_jid"], "message": body}, now)


def discard_draft(conn: sqlite3.Connection, message_id: int, *, now: int) -> dict[str, Any]:
    with db.write_txn(conn):
        row = _draft_row(conn, message_id)
        conn.execute("UPDATE messages SET status = 'discarded' WHERE id = ?", (message_id,))
        events.emit(
            conn,
            "message.status",
            now=now,
            account_id=row["account_id"],
            conversation_id=row["conversation_id"],
            message_id=message_id,
            payload={"status": "discarded"},
        )
    return conversations.get_message(conn, message_id)


# --- Receipts ----------------------------------------------------------------------


def apply_receipt(conn: sqlite3.Connection, account_id: int, item: dict, now: int) -> str:
    """Apply one bridge delivery receipt. Returns ``"applied"``, ``"unmatched"`` (no such message yet) or ``"ignored"``."""
    code = item.get("status")
    target = RECEIPT_STATUS.get(code) if isinstance(code, int) else None
    wa_id = item.get("id")
    if target is None or not isinstance(wa_id, str) or not wa_id:
        return "ignored"
    try:
        ts = int(item.get("ts") or 0)
    except (TypeError, ValueError):
        ts = 0
    if not 0 < ts <= now + 86400:
        ts = now
    with db.write_txn(conn):
        row = conn.execute(
            "SELECT id, conversation_id, account_id, status, delivered_at, read_at FROM messages"
            " WHERE account_id = ? AND wa_id = ? AND direction = 'out'",
            (account_id, wa_id),
        ).fetchone()
        if row is None:
            return "unmatched"
        current = STATUS_RANK.get(row["status"])
        if current is None or current >= STATUS_RANK[target]:
            return "ignored"
        delivered_at = row["delivered_at"] or (ts if target in ("delivered", "read", "played") else None)
        read_at = row["read_at"] or (ts if target in ("read", "played") else None)
        conn.execute(
            "UPDATE messages SET status = ?, delivered_at = ?, read_at = ?, error = NULL WHERE id = ?",
            (target, delivered_at, read_at, row["id"]),
        )
        events.emit(
            conn,
            "message.status",
            now=now,
            account_id=row["account_id"],
            conversation_id=row["conversation_id"],
            message_id=row["id"],
            payload={"status": target},
        )
    return "applied"


def _mark_read(conn: sqlite3.Connection, ids: list[int], now: int) -> None:
    with db.write_txn(conn):
        conn.executemany("UPDATE messages SET read_at = ? WHERE id = ?", [(now, i) for i in ids])


def send_read_receipts(conn: sqlite3.Connection, conversation_id: int, *, now: int) -> int:
    """Send WhatsApp read receipts (blue ticks) for the conversation's unread live inbound messages.

    Only the human UI route calls this. Returns the number of messages receipted; rows that could not be
    receipted keep ``read_at IS NULL`` and are retried on the next call.
    """
    try:
        conv, account = _prepare(conn, conversation_id)
    except errors.WaError:
        return 0
    rows = conn.execute(
        "SELECT id, wa_id, remote_jid FROM messages WHERE conversation_id = ? AND direction = 'in'"
        " AND source = 'live' AND wa_id IS NOT NULL AND read_at IS NULL ORDER BY ts, id LIMIT ?",
        (conversation_id, READ_RECEIPT_BATCH),
    ).fetchall()
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    if not settings.get_settings(conn).privacy.send_read_receipts:
        _mark_read(conn, ids, now)
        return 0
    keys = [{"remoteJid": r["remote_jid"] or conv["chat_jid"], "id": r["wa_id"], "fromMe": False} for r in rows]
    try:
        status, data = bridge.bridge_request(
            int(account["port"]), "POST", "/read", {"keys": keys}, timeout=READ_RECEIPT_TIMEOUT_SECONDS
        )
    except bridge.BridgeUnavailable:
        return 0
    if status != 200 or not isinstance(data, dict) or not data.get("success") or not data.get("marked"):
        return 0
    _mark_read(conn, ids, now)
    return len(ids)
