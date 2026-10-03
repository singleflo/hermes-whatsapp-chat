"""New chat: check numbers on WhatsApp and write to a number that has no conversation yet."""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Literal

from . import accounts, bridge, conversations, db, errors, events, outbound

MAX_CHECK = 50
MAX_NEW_CHATS_PER_HOUR = 10
CHECK_TIMEOUT_SECONDS = 20
STARTED_REASON = "conversation started"
MAX_NAME_CHARS = 120

_SEPARATORS = re.compile(r"[\s\-.()/]")
_DIGITS = re.compile(r"[0-9]+")
_NON_DIGITS = re.compile(r"[^0-9]")
PHONE_JID_SUFFIX = "@s.whatsapp.net"


# --- Numbers and accounts ----------------------------------------------------------


def normalize_phone(raw: str) -> str:
    """Digits with country code, no ``+``. National numbers are refused: the country is never guessed."""
    text = _SEPARATORS.sub("", str(raw or ""))
    if text.startswith("+"):
        digits = text[1:]
    elif text.startswith("00"):
        digits = text[2:]
    elif text.startswith("0"):
        raise errors.Invalid(
            f"{raw}: this looks like a national number, add the country code (e.g. +39 333 1234567)"
        )
    else:
        digits = text
        if _DIGITS.fullmatch(digits) and len(digits) < 11:
            raise errors.Invalid(f"{raw}: add the country code (e.g. +39 333 1234567)")
    if not _DIGITS.fullmatch(digits) or not 8 <= len(digits) <= 15:
        raise errors.Invalid(f"{raw} is not a valid phone number")
    return digits


def _linked(account: dict[str, Any]) -> bool:
    return account["desired"] == "running" and bool(account["paired"])


def pick_account(conn: sqlite3.Connection, account_id: int | None) -> dict[str, Any]:
    """The WhatsApp number to use: the explicit one, or the only one that is linked and running."""
    if account_id is not None:
        account = accounts.get_account(conn, account_id)
        if account["kind"] != "whatsapp":
            raise errors.Invalid("demo numbers can never send messages")
        if not _linked(account):
            raise errors.Unavailable(f"account '{account['label']}' is not paired and running")
        return account
    linked = [a for a in accounts.list_accounts(conn) if a["kind"] == "whatsapp" and _linked(a)]
    if not linked:
        raise errors.Unavailable("no WhatsApp number is linked and running")
    if len(linked) > 1:
        raise errors.Invalid("several WhatsApp numbers are linked: choose one (account_id / --account)")
    return linked[0]


def _own(account: dict[str, Any]) -> str | None:
    """Digits of the account's own number."""
    phone = _NON_DIGITS.sub("", str(account.get("phone") or ""))
    if phone:
        return phone
    jid = str(account.get("wa_jid") or "")
    return _NON_DIGITS.sub("", jid.split("@", 1)[0].split(":", 1)[0]) or None


def _existing(conn: sqlite3.Connection, account_id: int, digits: str, jid: str | None = None) -> sqlite3.Row | None:
    jids = [f"{digits}{PHONE_JID_SUFFIX}"] + ([jid] if jid else [])
    marks = ",".join("?" * len(jids))
    return conn.execute(
        f"SELECT * FROM conversations WHERE account_id = ? AND (chat_jid IN ({marks}) OR phone = ?)"
        " ORDER BY id LIMIT 1",
        (account_id, *jids, digits),
    ).fetchone()


# --- Bridge check ------------------------------------------------------------------


def _bridge_check(account: dict[str, Any], digits: list[str]) -> list[dict[str, Any]]:
    """Ask the number's bridge which of the phones are on WhatsApp (request order). No transaction open."""
    try:
        status, data = bridge.bridge_request(
            account["port"], "POST", "/check", {"phones": digits}, timeout=CHECK_TIMEOUT_SECONDS
        )
    except bridge.BridgeUnavailable as exc:
        raise errors.Unavailable(f"WhatsApp bridge of '{account['label']}' is not reachable") from exc
    data = data if isinstance(data, dict) else {}
    if status != 200:
        raise (errors.Unavailable if status == 503 else errors.BadGateway)(
            f"bridge check failed: {data.get('error') or status}"
        )
    results = data.get("results")
    if not isinstance(results, list) or len(results) != len(digits) or not all(isinstance(r, dict) for r in results):
        raise errors.BadGateway("bridge check failed: unexpected answer")
    return results


def check_numbers(conn: sqlite3.Connection, phones: list[str], account_id: int | None = None) -> dict[str, Any]:
    if not 1 <= len(phones) <= MAX_CHECK:
        raise errors.Invalid(f"check 1 to {MAX_CHECK} numbers at a time")
    digits = [normalize_phone(p) for p in phones]
    account = pick_account(conn, account_id)
    answers = _bridge_check(account, digits)
    own = _own(account)
    results = []
    for raw, number, answer in zip(phones, digits, answers):
        jid = answer.get("jid") if isinstance(answer.get("jid"), str) else None
        existing = _existing(conn, account["id"], number, jid)
        results.append(
            {
                "input": raw,
                "phone": f"+{number}",
                "exists": bool(answer.get("exists")),
                "jid": jid,
                "self": number == own,
                "conversation_id": existing["id"] if existing else None,
            }
        )
    return {"account_id": account["id"], "results": results}


# --- Start a conversation ----------------------------------------------------------


def _started_recently(conn: sqlite3.Connection, account_id: int, now: int) -> int:
    return conn.execute(
        "SELECT COUNT(DISTINCT l.conversation_id) FROM conversation_state_log l"
        " JOIN conversations c ON c.id = l.conversation_id"
        " WHERE c.account_id = ? AND l.reason = ? AND l.at >= ?",
        (account_id, STARTED_REASON, now - 3600),
    ).fetchone()[0]


def _create(
    conn: sqlite3.Connection, account_id: int, jid: str, name: str | None, actor: str, now: int
) -> tuple[int, bool]:
    """Insert the conversation (or find the one a concurrent call just made). Returns ``(id, created)``."""
    phone = _NON_DIGITS.sub("", jid.split("@", 1)[0].split(":", 1)[0]) or None
    with db.write_txn(conn):
        found = _existing(conn, account_id, phone or "", jid)
        if found is not None:
            return found["id"], False
        cur = conn.execute(
            "INSERT INTO conversations (account_id, chat_jid, contact_name, phone, state, unread_count,"
            " last_message_at, created_at, updated_at) VALUES (?,?,?,?,'in_progress',0,?,?,?)",
            (account_id, jid, name, phone, now, now, now),
        )
        conversation_id = cur.lastrowid or 0
        conn.execute(
            "INSERT INTO conversation_state_log (conversation_id, from_state, to_state, actor, reason, at)"
            " VALUES (?,NULL,'in_progress',?,?,?)",
            (conversation_id, actor, STARTED_REASON, now),
        )
        # Not conversation.created: that means a contact wrote first (notifications, "new conversation" rules).
        events.emit(
            conn,
            "conversation.updated",
            now=now,
            account_id=account_id,
            conversation_id=conversation_id,
            payload={"fields": ["created"], "origin": "outbound"},
        )
    return conversation_id, True


def _deliver(
    conn: sqlite3.Connection, conversation_id: int, text: str, mode: str, author: str, now: int
) -> dict[str, Any]:
    if mode == "send":
        return outbound.send_text(conn, conversation_id, text, author=author, now=now)
    return outbound.create_draft(conn, conversation_id, text, author=author, now=now)


def start_conversation(
    conn: sqlite3.Connection,
    *,
    phone: str,
    text: str,
    mode: Literal["send", "draft"],
    account_id: int | None = None,
    name: str | None = None,
    author: str,
    actor: str,
    now: int,
) -> dict[str, Any]:
    """Send (or draft) a first message to a phone, creating its conversation when there is none."""
    if not (text or "").strip():
        raise errors.Invalid("message text is empty")
    digits = normalize_phone(phone)
    account = pick_account(conn, account_id)
    if digits == _own(account):
        raise errors.Invalid(f"+{digits} is this account's own number")

    def finish(conversation_id: int, created: bool) -> dict[str, Any]:
        message = _deliver(conn, conversation_id, text, mode, author, now)
        detail = conversations.get_conversation(conn, conversation_id, now=now)["conversation"]
        return {"conversation": detail, "created": created, "message": message}

    existing = _existing(conn, account["id"], digits)
    if existing is not None:
        return finish(existing["id"], False)
    if _started_recently(conn, account["id"], now) >= MAX_NEW_CHATS_PER_HOUR:
        raise errors.TooMany(
            f"at most {MAX_NEW_CHATS_PER_HOUR} new chats per hour per number "
            "(protects the number from being blocked by WhatsApp); try again later"
        )
    answer = _bridge_check(account, [digits])[0]
    jid = answer.get("jid")
    if not answer.get("exists"):
        raise errors.NotOnWhatsApp(f"+{digits} is not on WhatsApp")
    if not isinstance(jid, str) or not jid.endswith(outbound.WA_JID_SUFFIXES):
        raise errors.BadGateway("bridge check failed: no WhatsApp address returned")
    existing = _existing(conn, account["id"], digits, jid)
    if existing is not None:
        return finish(existing["id"], False)
    clean_name = (name or "").strip()[:MAX_NAME_CHARS] or None
    conversation_id, created = _create(conn, account["id"], jid, clean_name, actor, now)
    return finish(conversation_id, created)
