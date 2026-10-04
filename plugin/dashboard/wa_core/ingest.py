"""Rules engine: turn one bridge message event into a stored message plus conversation changes."""

from __future__ import annotations

import json
import sqlite3
from typing import Any

from . import contacts, conversations, db, errors, events, lists, media, outbound, settings

NON_SUBSTANTIVE_MEDIA = {"reaction", "poll_update"}
WA_PHONE_SUFFIX = "@s.whatsapp.net"


def _jid_number(jid: str) -> str:
    return jid.split("@", 1)[0].split(":", 1)[0]


def canonical_jid(event: dict) -> str:
    """Conversation key: the contact's phone JID when the bridge provides one; a group's own chat JID.

    The bridge puts the phone JID in ``senderId`` when the chat is addressed by LID. In a group ``senderId`` is
    the author of the message, never the conversation.
    """
    if _is_group(event):
        return str(event.get("chatId") or "")
    own = {_jid_number(b) for b in event.get("botIds") or []}
    for candidate in (event.get("senderId"), event.get("chatId")):
        if candidate and candidate.endswith(WA_PHONE_SUFFIX) and _jid_number(candidate) not in own:
            return candidate
    return str(event.get("chatId") or "")


def event_ts(raw: Any, now: int) -> int:
    if isinstance(raw, dict):  # protobuf Long
        value = (int(raw.get("high", 0)) << 32) + (int(raw.get("low", 0)) & 0xFFFFFFFF)
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return now
    return value if 0 < value <= now + 86400 else now


def _is_group(event: dict) -> bool:
    """A group chat is recognised by its chat JID (the bridge's ``isGroup`` flag is derived from it)."""
    return str(event.get("chatId") or "").endswith("@g.us")


def _meta(event: dict) -> str | None:
    if not event.get("hasMedia"):
        return None
    return json.dumps(
        {"mediaType": event.get("mediaType"), "media": media.media_entries(event.get("mediaUrls") or [])}
    )


def _insert_message(
    conn: sqlite3.Connection,
    conv_id: int,
    account_id: int,
    *,
    wa_id: str | None,
    direction: str,
    author: str,
    body: str,
    ts: int,
    source: str,
    meta: str | None,
    remote_jid: str | None,
    sender_jid: str | None = None,
    sender_name: str | None = None,
    participant: str | None = None,
    read_at: int | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO messages (conversation_id, account_id, wa_id, direction, author, body, ts, status, source, meta,"
        " remote_jid, read_at, sender_jid, sender_name, participant) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            conv_id,
            account_id,
            wa_id,
            direction,
            author,
            body,
            ts,
            "received" if direction == "in" else "sent",
            source,
            meta,
            remote_jid,
            read_at,
            sender_jid,
            sender_name,
            participant,
        ),
    )
    return cur.lastrowid or 0


def _create_conversation(
    conn: sqlite3.Connection,
    account_id: int,
    jid: str,
    *,
    state: str,
    name: str | None,
    ts: int,
    now: int,
    unread: int,
    last_inbound_at: int | None,
    is_group: bool = False,
) -> sqlite3.Row:
    phone = _jid_number(jid) if jid.endswith(WA_PHONE_SUFFIX) else None
    cur = conn.execute(
        "INSERT INTO conversations (account_id, chat_jid, contact_name, phone, state, unread_count, last_message_at,"
        " last_inbound_at, created_at, updated_at, is_group) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (account_id, jid, name, phone, state, unread, ts, last_inbound_at, ts, now, 1 if is_group else 0),
    )
    lists.ensure_contact_list(conn, jid, {"id": account_id}, is_group=is_group, now=now)
    return conversations.get_row(conn, cur.lastrowid or 0)


def _announce_created(conn: sqlite3.Connection, row: sqlite3.Row, now: int) -> None:
    """New live conversation: audit row + conversation.created + state_changed (from null)."""
    conn.execute(
        "INSERT INTO conversation_state_log (conversation_id, from_state, to_state, actor, reason, at)"
        " VALUES (?,NULL,?,'auto','first message',?)",
        (row["id"], row["state"], now),
    )
    events.emit(
        conn,
        "conversation.created",
        now=now,
        account_id=row["account_id"],
        conversation_id=row["id"],
        payload=lists.event_flags(conn, row["id"], now),
    )
    events.emit(
        conn,
        "conversation.state_changed",
        now=now,
        account_id=row["account_id"],
        conversation_id=row["id"],
        payload={"from": None, "to": row["state"], "actor": "auto", "reason": "first message"},
    )


def _reopen_target(row: sqlite3.Row, rules: settings.Rules) -> tuple[str, str] | None:
    """Policy move for an inbound message on the current state, as (target, reason)."""
    state = row["state"]
    if state == "closed" and rules.inbound_on_closed != "keep":
        previous = row["previous_state"]
        if rules.inbound_on_closed == "reopen_previous" and previous in ("new", "in_progress", "waiting"):
            return previous, "rules.inbound_on_closed=reopen_previous"
        return "new", f"rules.inbound_on_closed={rules.inbound_on_closed}"
    if state == "waiting" and rules.inbound_on_waiting == "in_progress":
        return "in_progress", "rules.inbound_on_waiting=in_progress"
    if state == "muted" and rules.inbound_on_muted == "reopen_new":
        return "new", "rules.inbound_on_muted=reopen_new"
    return None


def _adopt_own_jid(conn: sqlite3.Connection, account_id: int, event: dict, now: int) -> None:
    """Migrated accounts have no number yet: take it from the bridge's own ids (wa_name is left alone).

    Runs inside the caller's transaction, so it cannot go through ``accounts.record_paired`` (own transaction).
    """
    own = next((b for b in event.get("botIds") or [] if isinstance(b, str) and b.endswith(WA_PHONE_SUFFIX)), None)
    if own is None:
        return
    conn.execute(
        "UPDATE accounts SET wa_jid = ?, phone = ?, updated_at = ? WHERE id = ? AND wa_jid IS NULL",
        (own, _jid_number(own), now, account_id),
    )


def _group_sender(account: sqlite3.Row, event: dict, from_owner: bool) -> dict[str, Any]:
    """Sender columns of a group message: canonical sender JID, display name and the raw participant (receipts)."""
    participant = str(event.get("participant") or "") or None
    if from_owner:
        own = contacts.own_number(account)
        return {"sender_jid": f"{own}{WA_PHONE_SUFFIX}" if own else None, "sender_name": None, "participant": participant}
    raw = str(event.get("senderId") or participant or "")
    name = str(event.get("senderName") or "").strip()
    return {
        "sender_jid": contacts.resolve_jid(account, raw) if raw else None,
        "sender_name": name if name and not name.lstrip("+").isdigit() else None,
        "participant": participant,
    }


def ingest_event(
    conn: sqlite3.Connection, account_id: int, event: dict, now: int, *, source: str = "live"
) -> int | None:
    """Store one bridge message event.

    Returns the new message id; None for system/self/broadcast chats, empty jids and duplicates. Groups are
    conversations keyed by their ``@g.us`` chat JID; each message carries its sender.
    """
    is_group = _is_group(event)
    jid = canonical_jid(event)
    if not jid:
        return None
    from_owner = bool(event.get("fromOwner"))
    ts = event_ts(event.get("timestamp"), now)
    body = str(event.get("body") or "")
    media_type = event.get("mediaType")
    wa_id = str(event.get("messageId") or "") or None
    meta = _meta(event)
    remote_jid = str(event.get("chatId") or "") or None
    direction = "out" if from_owner else "in"

    with db.write_txn(conn):
        account = conn.execute(
            "SELECT id, wa_jid, phone, session_dir FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        if account is None:
            raise errors.NotFound(f"account {account_id} not found")
        if account["wa_jid"] is None:
            _adopt_own_jid(conn, account_id, event, now)
            account = conn.execute(
                "SELECT id, wa_jid, phone, session_dir FROM accounts WHERE id = ?", (account_id,)
            ).fetchone()
        jid = contacts.resolve_jid(account, jid)
        if contacts.is_ignored_jid(jid, contacts.own_number(account)):
            return None
        if is_group:
            contact_name = None  # the subject comes from group metadata, never from a sender's name
        else:
            raw_name = event.get("contactName") or (None if from_owner else event.get("senderName"))
            contact_name = raw_name if raw_name and not str(raw_name).lstrip("+").isdigit() else None
        if wa_id and conn.execute(
            "SELECT 1 FROM messages WHERE account_id = ? AND wa_id = ?", (account_id, wa_id)
        ).fetchone():
            return None
        row = conn.execute(
            "SELECT * FROM conversations WHERE account_id = ? AND chat_jid = ?", (account_id, jid)
        ).fetchone()
        common: dict[str, Any] = {
            "wa_id": wa_id, "direction": direction, "body": body, "ts": ts, "meta": meta, "remote_jid": remote_jid,
        }
        if is_group:
            common.update(_group_sender(account, event, from_owner))

        if source == "history":
            if row is None:
                row = _create_conversation(
                    conn, account_id, jid, state="closed", name=contact_name, ts=ts, now=now, unread=0,
                    last_inbound_at=None, is_group=is_group,
                )
            elif contact_name:
                conn.execute(
                    "UPDATE conversations SET contact_name = COALESCE(contact_name, ?) WHERE id = ?",
                    (contact_name, row["id"]),
                )
            message_id = _insert_message(
                conn,
                row["id"],
                account_id,
                author="phone" if from_owner else "contact",
                source="history",
                read_at=None if from_owner else ts,
                **common,
            )
            conn.execute(
                "UPDATE conversations SET last_message_at = MAX(COALESCE(last_message_at, 0), ?) WHERE id = ?",
                (ts, row["id"]),
            )
            return message_id

        rules = settings.get_settings(conn).rules
        if from_owner:
            return _ingest_owner(conn, account_id, row, jid, common, rules, now, is_group=is_group)
        return _ingest_inbound(
            conn, account_id, row, jid, common, rules, contact_name, media_type, now, is_group=is_group
        )


def _ingest_inbound(
    conn: sqlite3.Connection,
    account_id: int,
    row: sqlite3.Row | None,
    jid: str,
    common: dict[str, Any],
    rules: settings.Rules,
    contact_name: str | None,
    media_type: str | None,
    now: int,
    *,
    is_group: bool = False,
) -> int:
    ts, body = common["ts"], common["body"]
    created = row is None
    if row is None:
        row = _create_conversation(
            conn, account_id, jid, state="new", name=contact_name, ts=ts, now=now, unread=1, last_inbound_at=ts,
            is_group=is_group,
        )
    message_id = _insert_message(conn, row["id"], account_id, author="contact", source="live", **common)
    if created:
        _announce_created(conn, row, now)
    else:
        conn.execute(
            "UPDATE conversations SET last_message_at = MAX(COALESCE(last_message_at, 0), ?), last_inbound_at = ?,"
            " unread_count = unread_count + 1, contact_name = COALESCE(?, contact_name), updated_at = ?"
            " WHERE id = ?",
            (ts, ts, contact_name, now, row["id"]),
        )
        substantive = bool(body.strip()) and media_type not in NON_SUBSTANTIVE_MEDIA
        move = _reopen_target(row, rules) if (row["state"] != "muted" or substantive) else None
        if move:
            conversations.force_transition(conn, row, move[0], actor="auto", reason=move[1], now=now)
    events.emit(
        conn,
        "message.in",
        now=now,
        account_id=account_id,
        conversation_id=row["id"],
        message_id=message_id,
        payload={"author": "contact", **lists.event_flags(conn, row["id"], now)},
    )
    return message_id


def _ingest_owner(
    conn: sqlite3.Connection,
    account_id: int,
    row: sqlite3.Row | None,
    jid: str,
    common: dict[str, Any],
    rules: settings.Rules,
    now: int,
    *,
    is_group: bool = False,
) -> int:
    ts = common["ts"]
    last_inbound = row["last_inbound_at"] if row is not None else None
    window = rules.auto_reply_window_seconds
    # WhatsApp Business away/greeting echoes exist only in direct chats; in a group the owner is a participant.
    is_auto = not is_group and last_inbound is not None and window > 0 and 0 <= ts - last_inbound <= window
    author = "auto_reply" if is_auto else "phone"
    created = row is None
    if row is None:
        row = _create_conversation(
            conn, account_id, jid, state="new" if is_auto else "waiting", name=None, ts=ts, now=now, unread=0,
            last_inbound_at=None, is_group=is_group,
        )
    message_id = _insert_message(conn, row["id"], account_id, author=author, source="live", **common)
    if created:
        _announce_created(conn, row, now)
    elif is_auto:
        conn.execute(
            "UPDATE conversations SET last_message_at = MAX(COALESCE(last_message_at, 0), ?), updated_at = ?"
            " WHERE id = ?",
            (ts, now, row["id"]),
        )
    else:
        conversations.apply_outbound(conn, row["id"], ts=ts, now=now, reason="reply sent from the phone")
    events.emit(
        conn,
        "message.out",
        now=now,
        account_id=account_id,
        conversation_id=row["id"],
        message_id=message_id,
        payload={"author": author},
    )
    return message_id


def ingest_update(conn: sqlite3.Connection, account_id: int, item: dict, now: int) -> str:
    """Apply one bridge ``/updates`` item. Returns ``"applied"``, ``"unmatched"`` (retry later) or ``"ignored"``."""
    kind = item.get("type") if isinstance(item, dict) else None
    if kind == "receipt":
        return outbound.apply_receipt(conn, account_id, item, now)
    if kind == "contact":
        return contacts.apply_contact(conn, account_id, item, now)
    return "ignored"
