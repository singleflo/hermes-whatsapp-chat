"""Jev exit conditions: TypeSafe Jev scores every incoming message against the user's exit conditions.

One call (``POST https://api.typesafe.ai/v1/systemone``) carries one ``noul`` question per condition and
answers with P(true) for each. The first condition in list order whose score reaches its minimum is the
exit, otherwise ``else``. The result is stored on the conversation and announced with the
``conversation.classified`` event, which automation rules turn into actions.

Enqueueing runs inside the caller's transaction (``events.emit``); the HTTP call (``process_due``) runs
OUTSIDE any transaction, like the automations executor.
"""

from __future__ import annotations

import json
import logging
import math
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable

from . import db, errors, events, settings

log = logging.getLogger("hermes_whatsapp_chat.jev")

JEV_URL = "https://api.typesafe.ai/v1/systemone"
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (5, 30)  # delay before attempt 2, before attempt 3
MAX_RESPONSE_BYTES = 1_048_576
KEY_ROW = "jev_api_key"
KEY_ENV = "TYPESAFE_API_KEY"
ELSE = "else"
CRITERIA = {"true": "The condition clearly applies now", "false": "The condition does not apply, or it is unclear"}
_SKIP_MEDIA = ("reaction", "poll_update")
_RETRY_STATUSES = (408, 429)
_BODY_CHARS = 500


# --- API key -------------------------------------------------------------------------


def api_key(conn) -> tuple[str | None, str | None]:
    """(key, source) with source ``settings`` | ``env`` | None. The key is never part of the settings object."""
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (KEY_ROW,)).fetchone()
    stored = db.jloads(row["value"], None) if row else None
    if isinstance(stored, str) and stored.strip():
        return stored.strip(), "settings"
    env = (os.environ.get(KEY_ENV) or "").strip()
    if env:
        return env, "env"
    return None, None


def key_status(conn) -> dict[str, Any]:
    key, source = api_key(conn)
    return {"key_set": key is not None, "key_source": source}


def set_api_key(conn, key: str, now: int) -> None:
    """Store the key; an empty key removes it."""
    key = (key or "").strip()
    with db.write_txn(conn):
        if not key:
            conn.execute("DELETE FROM settings WHERE key = ?", (KEY_ROW,))
            return
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (KEY_ROW, json.dumps(key), now),
        )


# --- HTTP boundary (test seam) --------------------------------------------------------


def _post(body: bytes, key: str, timeout: float) -> tuple[int, str, dict[str, str]]:
    """POST ``body`` to Jev; returns (status, text, lower-cased headers) for any HTTP status.

    Raises RuntimeError when Jev is unreachable, times out or answers more than MAX_RESPONSE_BYTES.
    """
    request = urllib.request.Request(
        JEV_URL,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "hermes-whatsapp-chat",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            status, raw, headers = resp.status, resp.read(MAX_RESPONSE_BYTES + 1), resp.headers
            lowered = {k.lower(): v for k, v in headers.items()}
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(MAX_RESPONSE_BYTES + 1)
        lowered = {k.lower(): v for k, v in exc.headers.items()}
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"Jev unreachable: {getattr(exc, 'reason', exc)}") from exc
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("Jev response too large")
    return status, raw.decode("utf-8", errors="replace"), lowered


# --- Request ---------------------------------------------------------------------------


def build_questions(cfg: settings.JevSettings) -> dict[str, Any]:
    return {
        e.id: {
            "type": "noul",
            "instructions": {"question": cfg.question, "condition": e.description},
            "criteria": CRITERIA,
        }
        for e in cfg.exits
    }


def _text_of(row) -> str:
    body = (row["body"] or "").strip()
    if not body:
        media = db.jloads(row["meta"], {}).get("mediaType") if row["meta"] else None
        return f"[{media}]" if media else ""
    return body[:_BODY_CHARS]


def _latest_inbound(conn, conversation_id: int):
    return conn.execute(
        "SELECT * FROM messages WHERE conversation_id = ? AND direction = 'in' AND status NOT IN ('draft','discarded')"
        " ORDER BY ts DESC, id DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()


def build_state(conn, cfg: settings.JevSettings, conv, message_id: int | None) -> dict[str, Any]:
    """What Jev reads: the number, the contact, the conversation, the latest and the recent messages."""
    account = conn.execute("SELECT label FROM accounts WHERE id = ?", (conv["account_id"],)).fetchone()
    latest = None
    if message_id is not None:
        latest = conn.execute(
            "SELECT * FROM messages WHERE id = ? AND conversation_id = ?", (message_id, conv["id"])
        ).fetchone()
    if latest is None:
        latest = _latest_inbound(conn, conv["id"])
    recent: list[str] = []
    if cfg.history_messages > 0:
        rows = conn.execute(
            "SELECT direction, body, meta FROM messages WHERE conversation_id = ?"
            " AND status NOT IN ('draft','discarded') ORDER BY ts DESC, id DESC LIMIT ?",
            (conv["id"], cfg.history_messages),
        ).fetchall()
        recent = [f"{'contact' if r['direction'] == 'in' else 'us'}: {_text_of(r)}" for r in reversed(rows)]
    digits = re.sub(r"\D", "", conv["phone"] or "")
    tags = db.jloads(conv["tags"], [])
    return {
        "number": account["label"] if account else "",
        "contact": {"name": conv["contact_name"] or None, "phone": f"+{digits}" if digits else None},
        "conversation": {
            "state": conv["state"],
            "tags": [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else [],
            "handled_by": "agent" if conv["agent_active"] else "person",
        },
        "latest_message": _text_of(latest) if latest is not None else "",
        "recent_messages": recent,
    }


def request_body(conn, cfg: settings.JevSettings, conv, message_id: int | None) -> bytes:
    body = {"state": build_state(conn, cfg, conv, message_id), "model": cfg.model, "questions": build_questions(cfg)}
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


# --- Answer ------------------------------------------------------------------------------


def parse_scores(cfg: settings.JevSettings, data: Any) -> dict[str, float]:
    """The P(true) of every exit; ValueError when the answer is not what was asked."""
    answers = data.get("answers") if isinstance(data, dict) else None
    if not isinstance(answers, dict):
        raise ValueError("Jev answer has no answers")
    scores: dict[str, float] = {}
    for e in cfg.exits:
        answer = answers.get(e.id)
        if not isinstance(answer, dict):
            raise ValueError(f"Jev answer is missing exit {e.id!r}")
        value = answer.get("noul")
        if answer.get("type") != "noul" or isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Jev answer for exit {e.id!r} is not a noul score")
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise ValueError(f"Jev score for exit {e.id!r} is out of range: {value}")
        scores[e.id] = float(value)
    return scores


def decide(cfg: settings.JevSettings, scores: dict[str, float]) -> tuple[str, float | None]:
    """First exit in list order whose score reaches its minimum; otherwise Else."""
    for e in cfg.exits:
        if scores[e.id] >= e.min_score:
            return e.id, scores[e.id]
    return ELSE, None


def summary_text(cfg: settings.JevSettings, classification: dict[str, Any]) -> str:
    """The chosen exit and every score, for prompts and the UI."""
    by_id = {e.id: e for e in cfg.exits}
    chosen = classification.get("exit")
    if chosen == ELSE or chosen is None:
        head = "Exit condition: Else (no condition reached its minimum score)"
    else:
        label = by_id[chosen].label if chosen in by_id else chosen
        head = f"Exit condition: {label} (score {float(classification.get('score') or 0):.2f})"
    lines = [head]
    for exit_id, score in (classification.get("scores") or {}).items():
        e = by_id.get(exit_id)
        minimum = f" (min {e.min_score:g})" if e else ""
        lines.append(f"- {e.label if e else exit_id}: {float(score):.2f}{minimum}")
    return "\n".join(lines)


# --- Enqueue -----------------------------------------------------------------------------


def _queue(conn, *, conversation_id: int, account_id: int, message_id: int | None, not_before: int, now: int) -> int:
    """One queued run per conversation: a newer message replaces the pending one and restarts its debounce."""
    row = conn.execute(
        "SELECT id FROM jev_runs WHERE conversation_id = ? AND status = 'queued' ORDER BY id DESC LIMIT 1",
        (conversation_id,),
    ).fetchone()
    if row is not None:
        conn.execute(
            "UPDATE jev_runs SET message_id = ?, not_before = ? WHERE id = ?", (message_id, not_before, row["id"])
        )
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO jev_runs (conversation_id, account_id, message_id, status, not_before, created_at)"
        " VALUES (?,?,?,'queued',?,?)",
        (conversation_id, account_id, message_id, not_before, now),
    )
    return int(cur.lastrowid or 0)


def enqueue_for_message(conn, *, conversation_id: int, message_id: int, now: int) -> int | None:
    """Queue a classification for a live inbound text message. Runs in the caller's transaction."""
    cfg = settings.get_settings(conn).jev
    if not cfg.enabled or not cfg.exits or api_key(conn)[0] is None:
        return None
    msg = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if msg is None or msg["source"] != "live" or msg["direction"] != "in" or not (msg["body"] or "").strip():
        return None
    if msg["conversation_id"] != conversation_id:
        return None
    meta = db.jloads(msg["meta"], {}) if msg["meta"] else {}
    if isinstance(meta, dict) and meta.get("mediaType") in _SKIP_MEDIA:
        return None
    account = conn.execute("SELECT kind FROM accounts WHERE id = ?", (msg["account_id"],)).fetchone()
    if account is None or account["kind"] != "whatsapp":
        return None
    if cfg.account_ids and msg["account_id"] not in cfg.account_ids:
        return None
    return _queue(
        conn,
        conversation_id=conversation_id,
        account_id=msg["account_id"],
        message_id=message_id,
        not_before=now + cfg.debounce_seconds,
        now=now,
    )


def _require_ready(conn) -> settings.JevSettings:
    """Settings usable for a manual call: a key and at least one exit."""
    cfg = settings.get_settings(conn).jev
    if api_key(conn)[0] is None:
        raise errors.Invalid("Jev has no API key")
    if not cfg.exits:
        raise errors.Invalid("Jev has no exit conditions")
    return cfg


def _conversation_row(conn, conversation_id: int):
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        raise errors.NotFound(f"conversation {conversation_id} not found")
    return row


def enqueue_now(conn, conversation_id: int, now: int) -> int:
    """Manual classification (Classify now): same coalescing, due immediately. The caller holds a transaction."""
    cfg = _require_ready(conn)
    if not cfg.enabled:
        raise errors.Invalid("Jev is disabled in settings")
    conv = _conversation_row(conn, conversation_id)
    msg = _latest_inbound(conn, conversation_id)
    if msg is None:
        raise errors.Invalid("conversation has no incoming message to classify")
    return _queue(
        conn, conversation_id=conversation_id, account_id=conv["account_id"], message_id=msg["id"], not_before=now, now=now
    )


# --- Synchronous scoring ---------------------------------------------------------------------


def score_state(cfg: settings.JevSettings, key: str, state: dict[str, Any]) -> dict[str, Any]:
    """Score ``state`` against ``cfg.exits`` right now; no database access, so it is safe in a thread."""
    body = json.dumps(
        {"state": state, "model": cfg.model, "questions": build_questions(cfg)}, ensure_ascii=False
    ).encode("utf-8")
    started = time.monotonic()
    try:
        status, text, _ = _post(body, key, cfg.timeout_s)
    except RuntimeError as exc:
        raise errors.Unavailable(str(exc)) from exc
    latency_ms = int((time.monotonic() - started) * 1000)
    if status != 200:
        raise errors.BadGateway(f"Jev HTTP {status}: {text[:300]}")
    try:
        data = json.loads(text)
        scores = parse_scores(cfg, data)
    except ValueError as exc:
        raise errors.BadGateway(str(exc)) from exc
    chosen, score = decide(cfg, scores)
    classification = {"exit": chosen, "score": score, "scores": scores}
    return {
        "state": state,
        "scores": scores,
        "exit": chosen,
        "score": score,
        "summary": summary_text(cfg, classification),
        "model": data.get("model") or cfg.model,
        "latency_ms": latency_ms,
        "usage": data.get("usage"),
    }


def text_state(text: str) -> dict[str, Any]:
    """A synthetic state for a message typed in the UI or CLI (no conversation behind it)."""
    return {
        "number": "",
        "contact": {"name": None, "phone": None},
        "conversation": {"state": "new", "tags": [], "handled_by": "agent"},
        "latest_message": text[:_BODY_CHARS],
        "recent_messages": ["contact: " + text[:_BODY_CHARS]],
    }


def _ready(conn, exits: list[settings.JevExit] | None) -> tuple[settings.JevSettings, str]:
    """Settings and key for a manual call; ``exits`` (a preview) replaces the stored conditions."""
    cfg = settings.get_settings(conn).jev
    key = api_key(conn)[0]
    if key is None:
        raise errors.Invalid("Jev has no API key")
    if exits is not None:
        try:
            cfg = cfg.model_copy(update={"exits": settings.JevSettings(exits=exits).exits})
        except ValueError as exc:
            raise errors.Invalid(f"invalid conditions: {exc}") from exc
    if not cfg.exits:
        raise errors.Invalid("Jev has no exit conditions")
    return cfg, key


def score_conversation(conn, conversation_id: int, exits: list[settings.JevExit] | None = None) -> dict[str, Any]:
    """Score a conversation's latest incoming message right now; nothing is stored."""
    cfg, key = _ready(conn, exits)
    conv = _conversation_row(conn, conversation_id)
    return score_state(cfg, key, build_state(conn, cfg, conv, None))


def score_text(conn, text: str, exits: list[settings.JevExit] | None = None) -> dict[str, Any]:
    """Score free text against ``exits`` (default: the stored ones); nothing is stored."""
    cfg, key = _ready(conn, exits)
    return score_state(cfg, key, text_state(text))


# --- Queue ---------------------------------------------------------------------------------


def _recover_stale(conn, now: int, stale_after: int) -> None:
    """Runs left `running` by a killed process go back to the queue, or fail after MAX_ATTEMPTS."""
    for row in conn.execute("SELECT id, attempts, started_at FROM jev_runs WHERE status = 'running'").fetchall():
        if row["started_at"] is None or now - row["started_at"] <= stale_after:
            continue
        if row["attempts"] < MAX_ATTEMPTS:
            conn.execute(
                "UPDATE jev_runs SET status = 'queued', not_before = NULL, error = 'interrupted while running' WHERE id = ?",
                (row["id"],),
            )
        else:
            conn.execute(
                "UPDATE jev_runs SET status = 'failed', error = 'interrupted while running', finished_at = ? WHERE id = ?",
                (now, row["id"]),
            )


def _claim(conn, now: int, limit: int) -> list[Any]:
    stale_after = settings.get_settings(conn).jev.timeout_s + 60
    with db.write_txn(conn):
        _recover_stale(conn, now, stale_after)
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM jev_runs WHERE status = 'queued' AND (not_before IS NULL OR not_before <= ?)"
                " ORDER BY id LIMIT ?",
                (now, limit),
            )
        ]
        for run_id in ids:
            conn.execute(
                "UPDATE jev_runs SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ?",
                (now, run_id),
            )
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return conn.execute(f"SELECT * FROM jev_runs WHERE id IN ({marks}) ORDER BY id", ids).fetchall()


def _end(conn, run, status: str, now: int, error: str | None = None) -> None:
    with db.write_txn(conn):
        conn.execute(
            "UPDATE jev_runs SET status = ?, error = ?, finished_at = ? WHERE id = ?", (status, error, now, run["id"])
        )


def _retry_or_fail(conn, run, now: int, error: str, retry_after: str | None = None) -> None:
    attempts = int(run["attempts"])
    if attempts >= MAX_ATTEMPTS:
        return _end(conn, run, "failed", now, error)
    delay = BACKOFF_SECONDS[attempts - 1]
    if retry_after:
        try:
            delay = max(1, min(300, int(float(retry_after))))
        except ValueError:
            pass
    with db.write_txn(conn):
        conn.execute(
            "UPDATE jev_runs SET status = 'queued', not_before = ?, error = ? WHERE id = ?", (now + delay, error, run["id"])
        )


def _store(
    conn,
    cfg: settings.JevSettings,
    run,
    scores: dict[str, float],
    *,
    model: str,
    latency_ms: int,
    input_tokens: int | None,
    now: int,
) -> None:
    chosen, score = decide(cfg, scores)
    with db.write_txn(conn):
        conv = conn.execute("SELECT id, account_id FROM conversations WHERE id = ?", (run["conversation_id"],)).fetchone()
        if conv is None:
            conn.execute(
                "UPDATE jev_runs SET status = 'skipped', error = 'conversation not found', finished_at = ? WHERE id = ?",
                (now, run["id"]),
            )
            return
        classification = {
            "exit": chosen,
            "score": score,
            "scores": scores,
            "model": model,
            "at": now,
            "run_id": run["id"],
            "message_id": run["message_id"],
            "latency_ms": latency_ms,
        }
        conn.execute(
            "UPDATE conversations SET classification = ? WHERE id = ?",
            (json.dumps(classification, ensure_ascii=False), conv["id"]),
        )
        conn.execute(
            "UPDATE jev_runs SET status = 'done', error = NULL, model = ?, latency_ms = ?, input_tokens = ?,"
            " finished_at = ? WHERE id = ?",
            (model, latency_ms, input_tokens, now, run["id"]),
        )
        events.emit(
            conn,
            "conversation.updated",
            now=now,
            account_id=conv["account_id"],
            conversation_id=conv["id"],
            payload={"fields": ["classification"]},
        )
        events.emit(
            conn,
            "conversation.classified",
            now=now,
            account_id=conv["account_id"],
            conversation_id=conv["id"],
            message_id=run["message_id"],
            payload={"run_id": run["id"], "message_id": run["message_id"], "exit": chosen, "score": score},
        )


def _execute(conn, run, now0: int, started: float) -> None:
    def clock() -> int:
        return now0 + int(time.monotonic() - started)

    cfg = settings.get_settings(conn).jev
    key = api_key(conn)[0]
    conv = conn.execute("SELECT * FROM conversations WHERE id = ?", (run["conversation_id"],)).fetchone()
    if not cfg.enabled:
        return _end(conn, run, "skipped", clock(), "Jev disabled")
    if key is None:
        return _end(conn, run, "skipped", clock(), "no API key")
    if not cfg.exits:
        return _end(conn, run, "skipped", clock(), "no exit conditions")
    if conv is None:
        return _end(conn, run, "skipped", clock(), "conversation not found")

    body = request_body(conn, cfg, conv, run["message_id"])
    call = time.monotonic()
    try:
        status, text, headers = _post(body, key, cfg.timeout_s)
    except RuntimeError as exc:
        return _retry_or_fail(conn, run, clock(), str(exc) or "Jev unreachable")
    latency_ms = int((time.monotonic() - call) * 1000)
    if status != 200:
        error = f"Jev HTTP {status}: {text[:300]}"
        if status in _RETRY_STATUSES or status >= 500:
            return _retry_or_fail(conn, run, clock(), error, headers.get("retry-after"))
        return _end(conn, run, "failed", clock(), error)
    try:
        data = json.loads(text)
        scores = parse_scores(cfg, data)
    except ValueError as exc:
        return _end(conn, run, "failed", clock(), str(exc) or "invalid Jev answer")
    usage = data.get("usage")
    tokens = usage.get("input_tokens") if isinstance(usage, dict) else None
    model = data.get("model") if isinstance(data.get("model"), str) else cfg.model
    _store(
        conn,
        cfg,
        run,
        scores,
        model=model,
        latency_ms=latency_ms,
        input_tokens=tokens if isinstance(tokens, int) and not isinstance(tokens, bool) else None,
        now=clock(),
    )


def process_due(connect_fn: Callable[[], Any], now: int, *, limit: int = 4) -> int:
    """Claim queued runs that are due, call Jev outside any transaction, record outcomes."""
    conn = connect_fn()
    try:
        runs = _claim(conn, now, limit)
        for run in runs:
            started = time.monotonic()
            try:
                _execute(conn, run, now, started)
            except Exception:
                log.exception("jev run %s could not be recorded", run["id"])
        return len(runs)
    finally:
        conn.close()
