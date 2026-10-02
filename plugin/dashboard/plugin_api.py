"""hermes-whatsapp-chat — backend API routes, mounted at /api/plugins/hermes-whatsapp-chat/.

Kanban board of WhatsApp conversations over the plugin's own SQLite DB. The
independent WhatsApp channel (sidecar/wa_channel.py) feeds it through
``ingest_event``; manual replies go out through the sidecar's Baileys bridge.
Modeled on the bundled kanban plugin: aggregate queries (no N+1), error mapping
helpers, every state change logged in an audit table inside the same transaction.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi import status as http_status
from pydantic import BaseModel

log = logging.getLogger(__name__)

router = APIRouter()

PLUGIN_ID = "hermes-whatsapp-chat"

# --- Board model ---------------------------------------------------------------

# Single source of column order. Keep exhaustive + fallback: a state missing
# here must land in the fallback column, never be dropped (kanban lesson).
BOARD_COLUMNS: list[str] = ["new", "in_progress", "waiting", "muted", "closed"]
FALLBACK_COLUMN = "in_progress"

CARD_PREVIEW_CHARS = 200
CLOSED_CARDS_LIMIT = 50
_EVENT_POLL_SECONDS = 1.0

# Allowed state transitions. Everything else -> 409 with the reason.
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "new": {"in_progress", "muted", "closed"},
    "in_progress": {"waiting", "muted", "closed"},
    "waiting": {"in_progress", "muted", "closed"},
    "muted": {"in_progress", "new"},
    "closed": {"in_progress"},
}

# --- Error mapping helpers (kanban shape) --------------------------------------


def _not_found(detail: str) -> HTTPException:
    return HTTPException(status_code=404, detail=detail)


def _conflict(detail: str) -> HTTPException:
    return HTTPException(status_code=409, detail=detail)


@contextmanager
def _map_errors(status: int, *types: type[BaseException]) -> Iterator[None]:
    try:
        yield
    except types as e:
        raise HTTPException(status_code=status, detail=str(e))


@contextmanager
def _errors_to_500(prefix: str) -> Iterator[None]:
    try:
        yield
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"{prefix}: {exc}")


# --- DB access -----------------------------------------------------------------

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS conversations (
  chat_jid TEXT PRIMARY KEY, contact_name TEXT, phone TEXT,
  state TEXT NOT NULL DEFAULT 'new', priority INTEGER NOT NULL DEFAULT 0,
  muted_until INTEGER, last_message_at INTEGER, unread_count INTEGER NOT NULL DEFAULT 0,
  agent_active INTEGER NOT NULL DEFAULT 1, tags TEXT NOT NULL DEFAULT '[]', updated_at INTEGER);
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_jid TEXT NOT NULL, wa_id TEXT,
  direction TEXT NOT NULL CHECK (direction IN ('in','out')), body TEXT, ts INTEGER NOT NULL, meta TEXT);
CREATE TABLE IF NOT EXISTS conversation_state_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_jid TEXT NOT NULL, from_state TEXT,
  to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_jid, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_wa_id ON messages(wa_id) WHERE wa_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conversations_state ON conversations(state);
CREATE INDEX IF NOT EXISTS idx_state_log_chat ON conversation_state_log(chat_jid, id);
"""


def _data_dir() -> Path:
    """Durable data root: ``<hermes home>/plugin-data/hermes-whatsapp-chat/``."""
    try:
        from plugins.plugin_storage import plugin_data_dir  # ty: ignore[unresolved-import]

        return plugin_data_dir(PLUGIN_ID)
    except ImportError:
        # Dev venv, sidecar, seed script: same layout, computed locally.
        root = Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "plugin-data" / PLUGIN_ID
        root.mkdir(parents=True, exist_ok=True)
        return root


def _db_path() -> Path:
    raw = os.environ.get("WA_ARCHIVE_DB")
    return Path(raw).expanduser() if raw else _data_dir() / "wa_board.db"


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def _write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def _tags(raw: str | None) -> list[str]:
    try:
        tags = json.loads(raw or "[]")
    except (ValueError, TypeError):
        return []
    return tags if isinstance(tags, list) else []


def _card(row: sqlite3.Row, now: int) -> dict[str, Any]:
    tags = _tags(row["tags"])
    last_at = row["last_message_at"]
    age = now - last_at if last_at is not None else None
    if row["priority"] >= 2 or "urgent" in tags:
        urgency = "high"
    elif age is not None and age > 86400 and row["state"] in ("new", "waiting"):
        urgency = "medium"
    else:
        urgency = "normal"
    return {
        "chat_jid": row["chat_jid"],
        "contact_name": row["contact_name"],
        "phone": row["phone"],
        "state": row["state"],
        "priority": row["priority"],
        "last_message_preview": (row["preview"] or "")[:CARD_PREVIEW_CHARS],
        "last_message_at": last_at,
        "age_seconds": age,
        "unread_count": row["unread_count"],
        "agent_active": bool(row["agent_active"]),
        "muted_until": row["muted_until"],
        "tags": tags,
        "urgency": urgency,
    }


def _log(
    conn: sqlite3.Connection,
    jid: str,
    from_state: str | None,
    to_state: str,
    reason: str | None,
    *,
    actor: str = "user",
    at: int,
) -> None:
    conn.execute(
        "INSERT INTO conversation_state_log (chat_jid, from_state, to_state, actor, reason, at) VALUES (?,?,?,?,?,?)",
        (jid, from_state, to_state, actor, reason, at),
    )


def _expire_mutes(conn: sqlite3.Connection, now: int) -> None:
    """Muted conversations whose snooze ended come back as ``new``."""
    due = "SELECT chat_jid FROM conversations WHERE state='muted' AND muted_until IS NOT NULL AND muted_until <= ?"
    if not conn.execute(due, (now,)).fetchone():
        return
    with _write_txn(conn):
        for r in conn.execute(due, (now,)).fetchall():
            conn.execute(
                "UPDATE conversations SET state='new', muted_until=NULL, updated_at=? WHERE chat_jid=?",
                (now, r["chat_jid"]),
            )
            _log(conn, r["chat_jid"], "muted", "new", "mute expired", actor="auto", at=now)


_LAST_BODY = (
    "(SELECT m.body FROM messages m WHERE m.chat_jid = c.chat_jid ORDER BY m.ts DESC, m.id DESC LIMIT 1)"
)
_LAST_DIRECTION = (
    "(SELECT m.direction FROM messages m WHERE m.chat_jid = c.chat_jid ORDER BY m.ts DESC, m.id DESC LIMIT 1)"
)


# --- WhatsApp bridge client ----------------------------------------------------

BRIDGE_PORT = 3017


class BridgeUnavailable(Exception):
    pass


def _bridge_port() -> int:
    return int(os.environ.get("WA_BRIDGE_PORT", BRIDGE_PORT))


def _bridge_request(
    method: str, path: str, payload: dict | None = None, *, timeout: float
) -> tuple[int, Any]:
    # The bridge accepts only loopback Host headers.
    url = f"http://127.0.0.1:{_bridge_port()}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except ValueError:
            return exc.code, None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BridgeUnavailable(f"bridge at {url} unreachable ({exc})")


def _channel_status() -> str:
    try:
        status, data = _bridge_request("GET", "/health", timeout=1.0)
    except BridgeUnavailable:
        return "unreachable"
    if status == 200 and isinstance(data, dict):
        return str(data.get("status") or "unreachable")
    return "unreachable"


# --- Message storage and ingest ------------------------------------------------

WA_JID_SUFFIXES = ("@s.whatsapp.net", "@lid")
NON_SUBSTANTIVE_MEDIA = {"reaction", "poll_update"}


def _store_message(
    conn: sqlite3.Connection,
    jid: str,
    direction: str,
    body: str,
    ts: int,
    wa_id: str | None,
    meta: str | None,
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages (chat_jid, wa_id, direction, body, ts, meta) VALUES (?,?,?,?,?,?)",
        (jid, wa_id, direction, body, ts, meta),
    )
    return cur.rowcount == 1


def _jid_number(jid: str) -> str:
    return jid.split("@", 1)[0].split(":", 1)[0]


def _canonical_jid(event: dict) -> str:
    """Conversation key: the contact's phone JID when the bridge provides one.

    The bridge puts the phone JID in ``senderId`` when the chat is addressed by LID.
    """
    own = {_jid_number(b) for b in event.get("botIds") or []}
    for candidate in (event.get("senderId"), event.get("chatId")):
        if candidate and candidate.endswith("@s.whatsapp.net") and _jid_number(candidate) not in own:
            return candidate
    return str(event.get("chatId") or "")


def _event_ts(raw: Any, now: int) -> int:
    if isinstance(raw, dict):  # protobuf Long
        value = (int(raw.get("high", 0)) << 32) + (int(raw.get("low", 0)) & 0xFFFFFFFF)
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError):
            return now
    return value if 0 < value <= now + 86400 else now


def ingest_event(conn: sqlite3.Connection, event: dict, now: int) -> bool:
    """Store one bridge message event. Returns False for groups, empty jids and duplicates."""
    if event.get("isGroup"):
        return False
    jid = _canonical_jid(event)
    if not jid:
        return False
    outbound = bool(event.get("fromOwner"))
    ts = _event_ts(event.get("timestamp"), now)
    body = str(event.get("body") or "")
    media_type = event.get("mediaType")
    meta = (
        json.dumps({"mediaType": media_type, "mediaUrls": event.get("mediaUrls") or []})
        if event.get("hasMedia")
        else None
    )
    phone = _jid_number(jid) if jid.endswith("@s.whatsapp.net") else None
    name = event.get("senderName")
    contact_name = name if not outbound and name and name != _jid_number(jid) else None

    with _write_txn(conn):
        if not _store_message(conn, jid, "out" if outbound else "in", body, ts, event.get("messageId") or None, meta):
            return False
        row = conn.execute("SELECT state FROM conversations WHERE chat_jid=?", (jid,)).fetchone()
        if row is None:
            state = "waiting" if outbound else "new"
            conn.execute(
                "INSERT INTO conversations (chat_jid, contact_name, phone, state, unread_count, last_message_at,"
                " updated_at) VALUES (?,?,?,?,?,?,?)",
                (jid, contact_name, phone, state, 0 if outbound else 1, ts, now),
            )
            _log(conn, jid, None, state, "first message", actor="auto", at=now)
        elif outbound:
            conn.execute(
                "UPDATE conversations SET last_message_at=MAX(COALESCE(last_message_at, 0), ?), unread_count=0,"
                " updated_at=? WHERE chat_jid=?",
                (ts, now, jid),
            )
        else:
            conn.execute(
                "UPDATE conversations SET last_message_at=MAX(COALESCE(last_message_at, 0), ?),"
                " unread_count=unread_count+1, contact_name=COALESCE(?, contact_name), updated_at=?"
                " WHERE chat_jid=?",
                (ts, contact_name, now, jid),
            )
            if row["state"] == "muted" and body.strip() and media_type not in NON_SUBSTANTIVE_MEDIA:
                conn.execute("UPDATE conversations SET state='new', muted_until=NULL WHERE chat_jid=?", (jid,))
                _log(conn, jid, "muted", "new", "new message while muted", actor="auto", at=now)
    return True


# --- GET /health ---------------------------------------------------------------


@router.get("/health")
def health() -> dict[str, Any]:
    try:
        with closing(_conn()) as conn:
            n = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
        return {"ok": True, "db": str(_db_path()), "conversations": n}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


# --- GET /board ----------------------------------------------------------------


@router.get("/board")
def get_board(include_closed: bool = Query(False), q: str = Query("")) -> dict[str, Any]:
    """Columns + cards. Two aggregate queries at most; the preview is a correlated subquery."""
    with _errors_to_500("board read failed"), closing(_conn()) as conn:
        now = int(time.time())
        _expire_mutes(conn, now)
        search, args = "", []
        if q.strip():
            search = " AND (c.contact_name LIKE ? OR c.phone LIKE ?)"
            args = [f"%{q.strip()}%"] * 2
        select = f"SELECT c.*, {_LAST_BODY} AS preview FROM conversations c WHERE "
        by_col: dict[str, list[dict]] = {name: [] for name in BOARD_COLUMNS}
        rows = conn.execute(
            select + "c.state != 'closed'" + search + " ORDER BY c.priority DESC, c.last_message_at ASC", args
        ).fetchall()
        for row in rows:
            column = row["state"] if row["state"] in by_col else FALLBACK_COLUMN
            by_col[column].append(_card(row, now))
        if include_closed:
            rows = conn.execute(
                select + "c.state = 'closed'" + search + f" ORDER BY c.last_message_at DESC LIMIT {CLOSED_CARDS_LIMIT}",
                args,
            ).fetchall()
            by_col["closed"] = [_card(row, now) for row in rows]
        return {"columns": [{"name": name, "cards": by_col[name]} for name in BOARD_COLUMNS], "now": now}


# --- GET /chats/{chat_jid} -----------------------------------------------------


def _get_conv(conn: sqlite3.Connection, jid: str) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM conversations WHERE chat_jid=?", (jid,)).fetchone()
    if row is None:
        raise _not_found(f"chat {jid} not found")
    return row


@router.get("/chats/{chat_jid}")
def get_chat(chat_jid: str) -> dict[str, Any]:
    with _errors_to_500("chat read failed"), closing(_conn()) as conn:
        row = _get_conv(conn, chat_jid)
        messages = conn.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE chat_jid=? ORDER BY ts DESC, id DESC LIMIT 100)"
            " ORDER BY ts, id",
            (chat_jid,),
        ).fetchall()
        history = conn.execute(
            "SELECT * FROM conversation_state_log WHERE chat_jid=? ORDER BY id DESC LIMIT 50", (chat_jid,)
        ).fetchall()
        allowed = ALLOWED_TRANSITIONS.get(row["state"], ALLOWED_TRANSITIONS[FALLBACK_COLUMN])
        return {
            "conversation": {**dict(row), "tags": _tags(row["tags"]), "agent_active": bool(row["agent_active"])},
            "messages": [dict(m) for m in messages],
            "state_history": [dict(h) for h in history],
            "allowed_states": sorted(allowed),
        }


# --- Guarded write routes ------------------------------------------------------


class StateBody(BaseModel):
    state: str
    reason: str | None = None
    muted_until: int | None = None


class MuteBody(BaseModel):
    muted_until: int
    reason: str | None = None


class EscalateBody(BaseModel):
    escalated: bool


class ReplyBody(BaseModel):
    text: str


def _transition(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    target: str,
    reason: str | None,
    muted_until: int | None,
    now: int,
) -> None:
    jid, current = row["chat_jid"], row["state"]
    if target not in BOARD_COLUMNS:
        raise HTTPException(status_code=400, detail=f"unknown state: {target}")
    allowed = ALLOWED_TRANSITIONS.get(current, ALLOWED_TRANSITIONS[FALLBACK_COLUMN])
    if target not in allowed:
        raise _conflict(f"cannot move {jid} from '{current}' to '{target}'; allowed from '{current}': {sorted(allowed)}")
    if target == "muted" and (muted_until is None or muted_until <= now):
        raise HTTPException(status_code=400, detail="muted_until must be a future unix timestamp")
    conn.execute(
        "UPDATE conversations SET state=?, muted_until=?, updated_at=? WHERE chat_jid=?",
        (target, muted_until if target == "muted" else None, now, jid),
    )
    _log(conn, jid, current, target, reason, at=now)


def _summary(conn: sqlite3.Connection, jid: str) -> dict[str, Any]:
    row = _get_conv(conn, jid)
    return {
        "ok": True,
        "chat_jid": jid,
        "state": row["state"],
        "agent_active": bool(row["agent_active"]),
        "priority": row["priority"],
        "muted_until": row["muted_until"],
        "unread_count": row["unread_count"],
    }


@router.post("/chats/{chat_jid}/state")
def set_state(chat_jid: str, payload: StateBody) -> dict[str, Any]:
    """Guarded transition: domain check + audit row in the SAME transaction."""
    with _errors_to_500("state change failed"), closing(_conn()) as conn:
        with _write_txn(conn):
            row = _get_conv(conn, chat_jid)
            _transition(conn, row, payload.state, payload.reason, payload.muted_until, int(time.time()))
        return _summary(conn, chat_jid)


@router.post("/chats/{chat_jid}/mute")
def mute(chat_jid: str, payload: MuteBody) -> dict[str, Any]:
    return set_state(
        chat_jid, StateBody(state="muted", muted_until=payload.muted_until, reason=payload.reason or "muted")
    )


@router.post("/chats/{chat_jid}/takeover")
def takeover(chat_jid: str) -> dict[str, Any]:
    """A human takes the conversation: state in_progress, agent_active off."""
    with _errors_to_500("takeover failed"), closing(_conn()) as conn:
        with _write_txn(conn):
            now = int(time.time())
            row = _get_conv(conn, chat_jid)
            moved = row["state"] != "in_progress"
            if moved:
                _transition(conn, row, "in_progress", "takeover", None, now)
            if row["agent_active"]:
                conn.execute("UPDATE conversations SET agent_active=0, updated_at=? WHERE chat_jid=?", (now, chat_jid))
                if not moved:
                    _log(conn, chat_jid, "in_progress", "in_progress", "takeover", at=now)
        return _summary(conn, chat_jid)


@router.post("/chats/{chat_jid}/handback")
def handback(chat_jid: str) -> dict[str, Any]:
    with _errors_to_500("handback failed"), closing(_conn()) as conn:
        with _write_txn(conn):
            now = int(time.time())
            row = _get_conv(conn, chat_jid)
            if not row["agent_active"]:
                conn.execute("UPDATE conversations SET agent_active=1, updated_at=? WHERE chat_jid=?", (now, chat_jid))
                _log(conn, chat_jid, row["state"], row["state"], "handback", at=now)
        return _summary(conn, chat_jid)


@router.post("/chats/{chat_jid}/escalate")
def escalate(chat_jid: str, payload: EscalateBody) -> dict[str, Any]:
    with _errors_to_500("escalate failed"), closing(_conn()) as conn:
        with _write_txn(conn):
            now = int(time.time())
            row = _get_conv(conn, chat_jid)
            priority = 2 if payload.escalated else 0
            if priority != row["priority"]:
                conn.execute("UPDATE conversations SET priority=?, updated_at=? WHERE chat_jid=?", (priority, now, chat_jid))
                _log(conn, chat_jid, row["state"], row["state"], "escalated" if payload.escalated else "de-escalated", at=now)
        return _summary(conn, chat_jid)


@router.post("/chats/{chat_jid}/read")
def mark_read(chat_jid: str) -> dict[str, Any]:
    with _errors_to_500("mark read failed"), closing(_conn()) as conn:
        with _write_txn(conn):
            row = _get_conv(conn, chat_jid)
            if row["unread_count"] > 0:
                conn.execute("UPDATE conversations SET unread_count=0 WHERE chat_jid=?", (chat_jid,))
        return _summary(conn, chat_jid)


@router.post("/chats/{chat_jid}/reply")
def reply(chat_jid: str, payload: ReplyBody) -> dict[str, Any]:
    """Send a manual reply through the WhatsApp bridge and record it as outbound."""
    text = payload.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="reply text is empty")
    if not chat_jid.endswith(WA_JID_SUFFIXES):
        raise HTTPException(status_code=400, detail=f"replies are only possible to WhatsApp chats (got {chat_jid})")
    with _errors_to_500("reply failed"), closing(_conn()) as conn:
        _get_conv(conn, chat_jid)
        try:
            status, data = _bridge_request("POST", "/send", {"chatId": chat_jid, "message": text}, timeout=70)
        except BridgeUnavailable as exc:
            raise HTTPException(status_code=503, detail=f"WhatsApp channel not running: {exc}")
        data = data if isinstance(data, dict) else {}
        if status != 200 or not data.get("success"):
            raise HTTPException(
                status_code=503 if status == 503 else 502,
                detail=f"WhatsApp send failed: {data.get('error') or status}",
            )
        now = int(time.time())
        with _write_txn(conn):
            _store_message(conn, chat_jid, "out", text, now, data.get("messageId"), None)
            conn.execute(
                "UPDATE conversations SET last_message_at=MAX(COALESCE(last_message_at, 0), ?), unread_count=0,"
                " updated_at=? WHERE chat_jid=?",
                (now, now, chat_jid),
            )
        return {**_summary(conn, chat_jid), "message_id": data.get("messageId")}


# --- GET /stats ----------------------------------------------------------------


@router.get("/stats")
def stats() -> dict[str, Any]:
    with _errors_to_500("stats failed"), closing(_conn()) as conn:
        now = int(time.time())
        _expire_mutes(conn, now)
        counts = {name: 0 for name in BOARD_COLUMNS}
        for r in conn.execute("SELECT state, COUNT(*) AS n FROM conversations GROUP BY state"):
            counts[r["state"]] = r["n"]
        oldest = conn.execute(
            "SELECT MIN(c.last_message_at) FROM conversations c WHERE c.state IN ('new', 'in_progress')"
            f" AND c.last_message_at IS NOT NULL AND {_LAST_DIRECTION} = 'in'"
        ).fetchone()[0]
    return {
        "counts": counts,
        "oldest_unanswered_age_seconds": now - oldest if oldest is not None else None,
        "channel": _channel_status(),
        "now": now,
    }


# --- WebSocket: /events?since=<state log id> -----------------------------------


def _ws_upgrade_authorized(ws: WebSocket) -> bool:
    """Authorize a WS upgrade via the dashboard's canonical gate (``web_server_chat._ws_auth_ok``:
    ``?token=`` / ``?ticket=`` / ``?internal=``) so this endpoint can never drift from core
    auth; accepts when the dashboard isn't importable (bare-FastAPI test harness)."""
    try:
        from hermes_cli import web_server_chat as _ws  # ty: ignore[unresolved-import]
    except Exception:
        return True
    return bool(_ws._ws_auth_ok(ws))


def _since_param(ws: WebSocket) -> int | None:
    """The client's event cursor, or None when it sent none (or garbage).

    None starts the stream at the current tail: a client that just opened the
    board already holds the snapshot. Only an explicit ``since`` replays history.
    """
    raw = ws.query_params.get("since")
    if raw is None or not str(raw).strip():
        return None
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return None


class _EventTail:
    """Per-socket tailer of the state log and the message table. One SQLite connection, used and
    closed only on a dedicated single-thread executor (connections are thread-affine)."""

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        self._executor: ThreadPoolExecutor | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = _conn()
        return self._conn

    def _latest(self) -> tuple[int, int]:
        conn = self._connection()
        log_max = conn.execute("SELECT COALESCE(MAX(id), 0) FROM conversation_state_log").fetchone()[0]
        msg_max = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        return log_max, msg_max

    def _fetch(self, cursor: int, msg_cursor: int) -> tuple[int, list[dict], int]:
        conn = self._connection()
        rows = conn.execute(
            "SELECT l.id, l.chat_jid, l.from_state, l.to_state, l.actor, l.reason, l.at, c.contact_name"
            " FROM conversation_state_log l LEFT JOIN conversations c ON c.chat_jid = l.chat_jid"
            " WHERE l.id > ? ORDER BY l.id LIMIT 200",
            (cursor,),
        ).fetchall()
        msg_max = conn.execute("SELECT COALESCE(MAX(id), 0) FROM messages").fetchone()[0]
        return (rows[-1]["id"] if rows else cursor), [dict(r) for r in rows], msg_max

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _run(self, fn, *args):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wa-chat-events")
        return asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    async def latest(self) -> tuple[int, int]:
        return await self._run(self._latest)

    async def poll(self, cursor: int, msg_cursor: int) -> tuple[int, list[dict], int]:
        return await self._run(self._fetch, cursor, msg_cursor)

    async def shutdown(self) -> None:
        if self._executor is None:
            return
        try:
            await asyncio.get_running_loop().run_in_executor(self._executor, self._close)
        except Exception as exc:
            log.warning("WhatsApp chat event stream connection cleanup failed: %s", exc)
        finally:
            self._executor.shutdown(wait=True, cancel_futures=True)


@router.websocket("/events")
async def stream_events(ws: WebSocket):
    if not _ws_upgrade_authorized(ws):
        await ws.close(code=http_status.WS_1008_POLICY_VIOLATION)
        return
    await ws.accept()
    tail = _EventTail()
    since = _since_param(ws)
    try:
        # Capture the tail at accept, before the first wait, so an event that lands in that
        # window is still delivered. A missing cursor must not mean 0 (that replays history).
        log_max, msg_cursor = await tail.latest()
        cursor = since if since is not None else log_max
        while True:
            # Race receive() against the poll interval so a disconnect is detected even when no
            # events flow. Other client messages are ignored.
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=_EVENT_POLL_SECONDS)
                if msg["type"] == "websocket.disconnect":
                    return
            except asyncio.TimeoutError:
                pass  # no client message — poll the DB
            cursor, events, msg_max = await tail.poll(cursor, msg_cursor)
            if events or msg_max != msg_cursor:
                msg_cursor = msg_max
                await ws.send_json({"events": events, "cursor": cursor, "message_cursor": msg_cursor})
    except WebSocketDisconnect:
        return
    except asyncio.CancelledError:
        return  # normal shutdown; CancelledError is a BaseException the handler below wouldn't quiet
    except Exception as exc:  # never crash the dashboard worker
        log.warning("WhatsApp chat event stream error: %s", exc)
        try:
            await ws.close()
        except Exception:
            pass
    finally:
        await tail.shutdown()
