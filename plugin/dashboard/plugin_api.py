"""hermes-whatsapp-chat — backend API routes, mounted at /api/plugins/hermes-whatsapp-chat/.

Multi-number WhatsApp chat over the plugin's own SQLite DB. All logic lives in the ``wa_core``
package next to this file (loaded by path, exposed as the module attribute ``core``); this module
only maps HTTP to it: request models, error mapping, and the live ``WS /events`` stream.
The sidecar (sidecar/wa_channel.py) feeds ``core.ingest`` and reconciles accounts from the DB;
``/service/*`` and ``/skill/*`` install it (LaunchAgent) and the Hermes skill from the UI.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import importlib.util
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

from fastapi import APIRouter, Body, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi import status as http_status
from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

PLUGIN_ID = "hermes-whatsapp-chat"
_CORE = "hermes_whatsapp_chat_core"
_EVENT_POLL_SECONDS = 1.0
_EVENT_BATCH = 200


def _load_core():
    # Fresh load on every import so tests and plugin rescans always see the current code.
    for name in [n for n in list(sys.modules) if n == _CORE or n.startswith(_CORE + ".")]:
        del sys.modules[name]
    pkg = Path(__file__).resolve().parent / "wa_core"
    spec = importlib.util.spec_from_file_location(_CORE, pkg / "__init__.py", submodule_search_locations=[str(pkg)])
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_CORE] = mod
    spec.loader.exec_module(mod)
    return mod


router = APIRouter()
core = _load_core()
router.include_router(core.automations_api.router)


# --- Error mapping ---------------------------------------------------------------


@contextmanager
def _session(prefix: str) -> Iterator[sqlite3.Connection]:
    """A DB connection for one request; domain errors map to their HTTP status, the rest to 500."""
    try:
        with closing(core.db.connect()) as conn:
            yield conn
    except HTTPException:
        raise
    except core.errors.WaError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc))
    except Exception as exc:
        log.exception("%s", prefix)
        raise HTTPException(status_code=500, detail=f"{prefix}: {exc}")


def _now() -> int:
    return int(time.time())


# --- Request bodies ----------------------------------------------------------------


class AccountCreate(BaseModel):
    label: str
    color: str = "blue"
    history_mode: str = "recent"


class AccountPatch(BaseModel):
    label: str | None = None
    color: str | None = None
    history_mode: str | None = None
    hermes_profile: str | None = None


class StateBody(BaseModel):
    state: str
    reason: str | None = None
    muted_until: int | None = None


class MuteBody(BaseModel):
    muted_until: int
    reason: str | None = None


class EscalateBody(BaseModel):
    escalated: bool


class TagsBody(BaseModel):
    tags: list[str]


class TextBody(BaseModel):
    text: str


class ReplyMediaBody(BaseModel):
    filename: str
    mime: str | None = None
    data_base64: str
    caption: str | None = None


class ApproveBody(BaseModel):
    text: str | None = None


# --- Health, service, accounts -----------------------------------------------------


@router.get("/health")
def health() -> dict[str, Any]:
    try:
        with closing(core.db.connect()) as conn:
            n = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            version = conn.execute("PRAGMA user_version").fetchone()[0]
        return {"ok": True, "db": str(core.db.db_path()), "conversations": n, "schema_version": version}
    except Exception as exc:
        return {"ok": False, "reason": str(exc)}


@router.get("/service")
def service() -> dict[str, Any]:
    with _session("service read failed") as conn:
        return core.accounts.service_info(conn, _now())


@router.post("/service/install")
def install_service() -> dict[str, Any]:
    with _session("service install failed") as conn:
        core.service.install_service(now=_now())
        return core.accounts.service_info(conn, _now())


@router.post("/service/uninstall")
def uninstall_service() -> dict[str, Any]:
    with _session("service uninstall failed") as conn:
        core.service.uninstall_service()
        return core.accounts.service_info(conn, _now())


@router.post("/skill/install")
def install_skill() -> dict[str, Any]:
    with _session("skill install failed"):
        return {"ok": True, **core.service.install_skill(now=_now())}


@router.post("/skill/uninstall")
def uninstall_skill() -> dict[str, Any]:
    with _session("skill uninstall failed"):
        return core.service.uninstall_skill()


@router.get("/accounts")
def list_accounts() -> dict[str, Any]:
    with _session("accounts read failed") as conn:
        return {"accounts": core.accounts.list_accounts(conn)}


@router.post("/accounts")
def create_account(payload: AccountCreate) -> dict[str, Any]:
    with _session("account create failed") as conn:
        return core.accounts.create_account(
            conn, label=payload.label, color=payload.color, history_mode=payload.history_mode, now=_now()
        )


@router.patch("/accounts/{account_id}")
def patch_account(account_id: int, payload: AccountPatch) -> dict[str, Any]:
    with _session("account update failed") as conn:
        fields = payload.model_dump(exclude_unset=True)
        return core.accounts.update_account(conn, account_id, now=_now(), **fields)


@router.post("/accounts/{account_id}/start")
def start_account(account_id: int) -> dict[str, Any]:
    with _session("account start failed") as conn:
        return core.accounts.set_desired(conn, account_id, "running", now=_now())


@router.post("/accounts/{account_id}/stop")
def stop_account(account_id: int) -> dict[str, Any]:
    with _session("account stop failed") as conn:
        return core.accounts.set_desired(conn, account_id, "stopped", now=_now())


@router.post("/accounts/{account_id}/restart")
def restart_account(account_id: int) -> dict[str, Any]:
    with _session("account restart failed") as conn:
        return core.accounts.request_restart(conn, account_id, now=_now())


@router.post("/accounts/{account_id}/logout")
def logout_account(account_id: int) -> dict[str, Any]:
    with _session("account logout failed") as conn:
        return core.accounts.set_desired(conn, account_id, "logged_out", now=_now())


@router.delete("/accounts/{account_id}")
def delete_account(account_id: int, delete_conversations: bool = Query(False)) -> dict[str, Any]:
    with _session("account removal failed") as conn:
        core.accounts.remove_account(conn, account_id, delete_conversations=delete_conversations, now=_now())
        return {"ok": True}


# --- Settings ----------------------------------------------------------------------


@router.get("/settings")
def get_settings() -> dict[str, Any]:
    with _session("settings read failed") as conn:
        return core.settings.get_settings(conn).model_dump()


@router.put("/settings")
def put_settings(payload: dict[str, Any] = Body(...)) -> dict[str, Any]:
    try:
        parsed = core.settings.Settings.model_validate(payload)
    except ValidationError as exc:
        detail = "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
        raise HTTPException(status_code=422, detail=detail)
    with _session("settings save failed") as conn:
        return core.settings.save_settings(conn, parsed, _now()).model_dump()


# --- Board, list, detail, messages, search -------------------------------------------


@router.get("/board")
def get_board(
    account_id: int | None = Query(None), q: str = Query(""), include_closed: bool = Query(False)
) -> dict[str, Any]:
    with _session("board read failed") as conn:
        return core.conversations.get_board(
            conn, account_id=account_id, q=q, include_closed=include_closed, now=_now()
        )


@router.get("/conversations")
def list_conversations(
    account_id: int | None = Query(None),
    state: str | None = Query(None),
    q: str = Query(""),
    unread_only: bool = Query(False),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
) -> dict[str, Any]:
    with _session("conversation list failed") as conn:
        return core.conversations.list_conversations(
            conn,
            account_id=account_id,
            state=state,
            q=q,
            unread_only=unread_only,
            limit=limit,
            offset=offset,
            now=_now(),
        )


@router.get("/conversations/{conversation_id}")
def get_conversation(conversation_id: int) -> dict[str, Any]:
    with _session("conversation read failed") as conn:
        return core.conversations.get_conversation(conn, conversation_id, now=_now())


@router.get("/conversations/{conversation_id}/messages")
def list_messages(
    conversation_id: int, before_id: int | None = Query(None), limit: int = Query(50, ge=1, le=200)
) -> dict[str, Any]:
    with _session("message list failed") as conn:
        return core.conversations.list_messages(conn, conversation_id, before_id=before_id, limit=limit)


@router.get("/messages/{message_id}/media/{index}")
def get_media(message_id: int, index: int) -> dict[str, Any]:
    with _session("media read failed") as conn:
        return core.media.media_data_url(conn, message_id, index)


@router.get("/search")
def search(q: str = Query(""), account_id: int | None = Query(None)) -> dict[str, Any]:
    with _session("search failed") as conn:
        return core.conversations.search_messages(conn, q, account_id=account_id, now=_now())


# --- Guarded write routes ------------------------------------------------------------


@router.post("/conversations/{conversation_id}/state")
def set_state(conversation_id: int, payload: StateBody) -> dict[str, Any]:
    with _session("state change failed") as conn:
        return core.conversations.set_state(
            conn,
            conversation_id,
            payload.state,
            now=_now(),
            reason=payload.reason,
            muted_until=payload.muted_until,
        )


@router.post("/conversations/{conversation_id}/mute")
def mute(conversation_id: int, payload: MuteBody) -> dict[str, Any]:
    with _session("mute failed") as conn:
        return core.conversations.mute(conn, conversation_id, payload.muted_until, now=_now(), reason=payload.reason)


@router.post("/conversations/{conversation_id}/takeover")
def takeover(conversation_id: int) -> dict[str, Any]:
    with _session("takeover failed") as conn:
        return core.conversations.takeover(conn, conversation_id, now=_now())


@router.post("/conversations/{conversation_id}/handback")
def handback(conversation_id: int) -> dict[str, Any]:
    with _session("handback failed") as conn:
        return core.conversations.handback(conn, conversation_id, now=_now())


@router.post("/conversations/{conversation_id}/escalate")
def escalate(conversation_id: int, payload: EscalateBody) -> dict[str, Any]:
    with _session("escalate failed") as conn:
        return core.conversations.escalate(conn, conversation_id, payload.escalated, now=_now())


@router.post("/conversations/{conversation_id}/read")
def mark_read(conversation_id: int) -> dict[str, Any]:
    with _session("mark read failed") as conn:
        return core.conversations.mark_read(conn, conversation_id, now=_now())


@router.put("/conversations/{conversation_id}/tags")
def set_tags(conversation_id: int, payload: TagsBody) -> dict[str, Any]:
    with _session("tag update failed") as conn:
        return core.conversations.set_tags(conn, conversation_id, payload.tags, now=_now())


# --- Outbound ------------------------------------------------------------------------


@router.post("/conversations/{conversation_id}/reply")
def reply(conversation_id: int, payload: TextBody) -> dict[str, Any]:
    with _session("reply failed") as conn:
        message = core.outbound.send_text(conn, conversation_id, payload.text, author="user", now=_now())
        return {"summary": core.conversations.summary(conn, conversation_id), "message": message}


@router.post("/conversations/{conversation_id}/reply-media")
def reply_media(conversation_id: int, payload: ReplyMediaBody) -> dict[str, Any]:
    try:
        data = base64.b64decode(payload.data_base64, validate=True)
    except (binascii.Error, ValueError):
        raise HTTPException(status_code=400, detail="data_base64 is not valid base64")
    with _session("media reply failed") as conn:
        message = core.outbound.send_media(
            conn,
            conversation_id,
            filename=payload.filename,
            mime=payload.mime,
            data=data,
            caption=payload.caption,
            author="user",
            now=_now(),
        )
        return {"summary": core.conversations.summary(conn, conversation_id), "message": message}


@router.post("/conversations/{conversation_id}/drafts")
def create_draft(conversation_id: int, payload: TextBody) -> dict[str, Any]:
    with _session("draft failed") as conn:
        return core.outbound.create_draft(conn, conversation_id, payload.text, author="user", now=_now())


@router.post("/messages/{message_id}/approve")
def approve_draft(message_id: int, payload: ApproveBody | None = None) -> dict[str, Any]:
    with _session("draft approval failed") as conn:
        text = payload.text if payload is not None else None
        return core.outbound.approve_draft(conn, message_id, text=text, now=_now())


@router.post("/messages/{message_id}/discard")
def discard_draft(message_id: int) -> dict[str, Any]:
    with _session("draft discard failed") as conn:
        return core.outbound.discard_draft(conn, message_id, now=_now())


# --- WebSocket: /events?since=<event id> ---------------------------------------------


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
    """Per-socket tailer of the ``events`` table. One SQLite connection, used and closed only on a
    dedicated single-thread executor (connections are thread-affine)."""

    def __init__(self) -> None:
        self._conn: sqlite3.Connection | None = None
        self._executor: ThreadPoolExecutor | None = None

    def _connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = core.db.connect()
        return self._conn

    def _latest(self) -> int:
        return self._connection().execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]

    def _fetch(self, cursor: int) -> tuple[int, list[dict]]:
        rows = (
            self._connection()
            .execute(
                "SELECT e.*, c.contact_name FROM events e LEFT JOIN conversations c ON c.id = e.conversation_id"
                " WHERE e.id > ? ORDER BY e.id LIMIT ?",
                (cursor, _EVENT_BATCH),
            )
            .fetchall()
        )
        out = [{**dict(r), "payload": core.db.jloads(r["payload"], None)} for r in rows]
        return (rows[-1]["id"] if rows else cursor), out

    def _close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _run(self, fn, *args):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="wa-chat-events")
        return asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)

    async def latest(self) -> int:
        return await self._run(self._latest)

    async def poll(self, cursor: int) -> tuple[int, list[dict]]:
        return await self._run(self._fetch, cursor)

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
        latest = await tail.latest()
        cursor = since if since is not None else latest
        while True:
            # Race receive() against the poll interval so a disconnect is detected even when no
            # events flow. Other client messages are ignored.
            try:
                msg = await asyncio.wait_for(ws.receive(), timeout=_EVENT_POLL_SECONDS)
                if msg["type"] == "websocket.disconnect":
                    return
            except asyncio.TimeoutError:
                pass  # no client message — poll the DB
            cursor, events = await tail.poll(cursor)
            if events:
                await ws.send_json({"events": events, "cursor": cursor})
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
