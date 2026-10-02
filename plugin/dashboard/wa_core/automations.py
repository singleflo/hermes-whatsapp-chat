"""Automations: rule matching (called from ``events.emit``), run queue and executor.

Matching runs inside the caller's transaction and only inserts ``automation_runs`` rows.
Execution (``process_due``) claims runs in a short transaction, performs the action
(subprocess / HTTP / SQL) OUTSIDE any transaction, then records the outcome.
"""

from __future__ import annotations

import functools
import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

from . import db, errors, events, outbound, settings

log = logging.getLogger("hermes_whatsapp_chat.automations")

TRIGGER_TYPES = ("message.in", "message.out", "conversation.created", "conversation.state_changed")
STATES = ("new", "in_progress", "waiting", "muted", "closed")
REPLY_ACTIONS = ("hermes", "webhook", "script", "reply")
MAX_ATTEMPTS = 3
BACKOFF_SECONDS = (30, 120)  # delay before attempt 2, before attempt 3
DEFAULT_STALE_SECONDS = 300  # running longer than this without a known timeout_s counts as interrupted
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_LOOP_PREFIXES = ("agent:", "rule:")
_SESSION_LINE = re.compile(r"^\s*session_id:\s*\S+\s*$")
_TEMPLATE_KEY = re.compile(r"\{(\w+)\}")


class _Skip(Exception):
    """The run must end as ``skipped`` with this reason."""


class _Permanent(Exception):
    """Failure that retrying cannot fix."""


# --- Rules -----------------------------------------------------------------------


def rule_to_dict(row) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "enabled": bool(row["enabled"]),
        "position": row["position"],
        "account_id": row["account_id"],
        "event_types": db.jloads(row["event_types"], []),
        "conditions": db.jloads(row["conditions"], {}),
        "action": db.jloads(row["action"], {}),
        "reply_mode": row["reply_mode"],
        "stop_after_match": bool(row["stop_after_match"]),
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _cfg(conn) -> dict[str, Any]:
    return settings.get_settings(conn).model_dump()


def _zone(name: str | None) -> ZoneInfo:
    try:
        return ZoneInfo(name or "UTC")
    except Exception:
        return ZoneInfo("UTC")


# --- Context and condition evaluation --------------------------------------------


def _conversation(conn, conversation_id: int | None) -> dict[str, Any] | None:
    if conversation_id is None:
        return None
    row = conn.execute("SELECT * FROM conversations WHERE id = ?", (conversation_id,)).fetchone()
    if row is None:
        return None
    conv = dict(row)
    conv["tags"] = [t for t in db.jloads(row["tags"], []) if isinstance(t, str)]
    return conv


def _latest_message(conn, conversation_id: int, direction: str | None = None):
    sql = "SELECT * FROM messages WHERE conversation_id = ? AND status NOT IN ('draft','discarded')"
    args: list[Any] = [conversation_id]
    if direction:
        sql += " AND direction = ?"
        args.append(direction)
    return conn.execute(sql + " ORDER BY ts DESC, id DESC LIMIT 1", args).fetchone()


def _context(conn, event_type: str, conv: dict[str, Any], msg, payload: dict[str, Any]) -> dict[str, Any]:
    """What conditions look at: the triggering message, or the latest one for conversation events."""
    ref = msg if msg is not None else _latest_message(conn, conv["id"])
    return {
        "event_type": event_type,
        "conv": conv,
        "msg": msg,
        "payload": payload,
        "text": (ref["body"] or "") if ref is not None else "",
        "direction": ref["direction"] if ref is not None else None,
    }


def loop_guard_reason(event_type: str, msg, payload: dict[str, Any]) -> str | None:
    """Why this event must never trigger automations (None = fine)."""
    if event_type in ("message.in", "message.out"):
        if msg is None:
            return "event has no message"
        if msg["source"] == "history":
            return "history messages never trigger automations"
        if str(msg["author"]).startswith(_LOOP_PREFIXES):
            return f"message author {msg['author']} is an automation (loop guard)"
    elif event_type == "conversation.state_changed":
        if str(payload.get("actor") or "").startswith(_LOOP_PREFIXES):
            return f"state changed by {payload.get('actor')} (loop guard)"
    return None


def _hm(value: Any) -> int | None:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2))


def in_business_hours(cfg: dict[str, Any], ts: int) -> bool:
    hours = cfg.get("hours") or {}
    local = datetime.fromtimestamp(ts, _zone(hours.get("timezone")))
    windows = (hours.get("business") or {}).get(_WEEKDAYS[local.weekday()]) or []
    minute = local.hour * 60 + local.minute
    for window in windows:
        try:
            start, end = _hm(window[0]), _hm(window[1])
        except (TypeError, IndexError, KeyError):
            continue
        if start is None or end is None:
            continue
        if start <= end:
            if start <= minute < end:
                return True
        elif minute >= start or minute < end:  # window crossing midnight
            return True
    return False


def _first_message(conn, ctx: dict[str, Any]) -> bool:
    etype, msg = ctx["event_type"], ctx["msg"]
    if etype == "conversation.created":
        return True
    if msg is None:
        return False
    count = conn.execute(
        "SELECT COUNT(*) FROM messages WHERE conversation_id = ? AND direction = ? AND status NOT IN ('draft','discarded')",
        (ctx["conv"]["id"], msg["direction"]),
    ).fetchone()[0]
    return count == 1


def evaluate(conn, cfg: dict[str, Any], rule: dict[str, Any], ctx: dict[str, Any], now: int) -> list[tuple[bool, str]]:
    """Every check of a rule against a context as (passed, explanation)."""
    conv, cond = ctx["conv"], rule["conditions"] or {}
    out: list[tuple[bool, str]] = []
    text = ctx["text"]

    if rule["account_id"] is not None:
        ok = rule["account_id"] == conv["account_id"]
        out.append((ok, f"account {conv['account_id']} {'matches' if ok else 'is not'} rule account {rule['account_id']}"))
    ok = ctx["event_type"] in rule["event_types"]
    out.append((ok, f"event {ctx['event_type']} {'is' if ok else 'is not'} in {rule['event_types']}"))

    if cond.get("states") is not None:
        ok = conv["state"] in cond["states"]
        out.append((ok, f"state is {conv['state']}; rule wants {cond['states']}"))
    if cond.get("agent_active") is not None:
        ok = bool(conv["agent_active"]) == bool(cond["agent_active"])
        out.append((ok, f"agent_active is {bool(conv['agent_active'])}; rule wants {bool(cond['agent_active'])}"))
    if cond.get("business_hours") in ("in", "out"):
        inside = in_business_hours(cfg, now)
        ok = inside == (cond["business_hours"] == "in")
        out.append((ok, f"currently {'inside' if inside else 'outside'} business hours; rule wants {cond['business_hours']}"))
    if cond.get("text_contains"):
        lowered = text.lower()
        ok = any(str(needle).lower() in lowered for needle in cond["text_contains"] if str(needle))
        out.append((ok, f"text {'contains' if ok else 'does not contain'} any of {cond['text_contains']}"))
    if cond.get("text_regex"):
        try:
            ok = re.search(cond["text_regex"], text, re.IGNORECASE) is not None
            out.append((ok, f"text {'matches' if ok else 'does not match'} /{cond['text_regex']}/"))
        except re.error as exc:
            out.append((False, f"invalid text_regex: {exc}"))
    if cond.get("tags_any"):
        have = {t.lower() for t in conv["tags"]}
        ok = any(str(t).lower() in have for t in cond["tags_any"])
        out.append((ok, f"tags {conv['tags']} {'include' if ok else 'do not include'} any of {cond['tags_any']}"))
    if cond.get("first_message") is not None:
        first = _first_message(conn, ctx)
        ok = first == bool(cond["first_message"])
        out.append((ok, f"is first message: {first}; rule wants {bool(cond['first_message'])}"))
    if cond.get("directions"):
        ok = ctx["direction"] in cond["directions"]
        out.append((ok, f"direction {ctx['direction']} {'is' if ok else 'is not'} in {cond['directions']}"))
    return out


def enqueue_for_event(
    conn,
    *,
    event_id: int,
    type: str,
    now: int,
    conversation_id: int | None,
    message_id: int | None,
    payload: dict[str, Any],
) -> list[int]:
    """Insert a run for every enabled rule matching this event. Runs in the caller's transaction."""
    cfg = _cfg(conn)
    auto = cfg["automations"]
    if not auto["enabled"]:
        return []
    conv = _conversation(conn, conversation_id)
    if conv is None:
        return []
    msg = None
    if message_id is not None:
        msg = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    if loop_guard_reason(type, msg, payload) is not None:
        return []
    ctx = _context(conn, type, conv, msg, payload)
    limit = int(auto["max_runs_per_conversation_per_hour"])
    created: list[int] = []
    rules = conn.execute("SELECT * FROM automation_rules WHERE enabled = 1 ORDER BY position, id").fetchall()
    for row in rules:
        rule = rule_to_dict(row)
        if not all(ok for ok, _ in evaluate(conn, cfg, rule, ctx, now)):
            continue
        used = conn.execute(
            "SELECT COUNT(*) FROM automation_runs WHERE conversation_id = ? AND created_at >= ? AND status != 'skipped'",
            (conv["id"], now - 3600),
        ).fetchone()[0]
        if used >= limit:
            cur = conn.execute(
                "INSERT INTO automation_runs (rule_id, event_id, conversation_id, status, error, created_at, finished_at)"
                " VALUES (?,?,?,'skipped','rate limited',?,?)",
                (rule["id"], event_id, conv["id"], now, now),
            )
        else:
            cur = conn.execute(
                "INSERT INTO automation_runs (rule_id, event_id, conversation_id, status, created_at)"
                " VALUES (?,?,?,'queued',?)",
                (rule["id"], event_id, conv["id"], now),
            )
        created.append(int(cur.lastrowid or 0))
        if rule["stop_after_match"]:
            break
    return created


def dry_run(conn, rule: dict[str, Any], conversation_id: int, now: int) -> dict[str, Any]:
    """Would this rule fire for this conversation's latest message? No side effects."""
    conv = _conversation(conn, conversation_id)
    if conv is None:
        raise errors.NotFound(f"conversation {conversation_id} not found")
    cfg = _cfg(conn)
    result: dict[str, Any] | None = None
    for etype in rule["event_types"] or ["message.in"]:
        msg = None
        notes: list[tuple[bool, str]] = []
        if etype in ("message.in", "message.out"):
            msg = _latest_message(conn, conv["id"], "in" if etype == "message.in" else "out")
            if msg is None:
                notes.append((False, f"conversation has no {'inbound' if etype == 'message.in' else 'outbound'} message to simulate"))
        payload: dict[str, Any] = {}
        guard = loop_guard_reason(etype, msg, payload) if msg is not None else None
        if guard:
            notes.append((False, guard))
        ctx = _context(conn, etype, conv, msg, payload)
        checks = notes + evaluate(conn, cfg, rule, ctx, now)
        if not cfg["automations"]["enabled"]:
            checks.append((False, "automations are disabled in settings"))
        matches = all(ok for ok, _ in checks)
        reasons = [("ok: " if ok else "no: ") + text for ok, text in checks]
        if not rule["enabled"]:
            reasons.append("note: the rule is disabled and will not fire until enabled")
        rendered = None
        action = rule["action"]
        variables = _template_vars(conn, cfg, conv, ctx["text"])
        if action.get("type") == "hermes":
            rendered = render_template(action.get("prompt", ""), variables)
        elif action.get("type") == "reply":
            rendered = render_template(action.get("text", ""), variables)
        result = {"matches": matches, "reasons": reasons, "rendered_prompt": rendered}
        if matches:
            break
    assert result is not None
    return result


# --- Templates --------------------------------------------------------------------


@functools.lru_cache(maxsize=1)
def wa_cli_command() -> str:
    """Absolute command running scripts/wa.py from the dev repo; plain ``wa.py`` when not found."""
    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file():
            return f"{shlex.quote(str(parent / '.venv' / 'bin' / 'python'))} {shlex.quote(str(parent / 'scripts' / 'wa.py'))}"
    return "wa.py"


def render_template(template: str, variables: dict[str, Any]) -> str:
    """Replace ``{name}``; unknown names render empty, nothing is re-expanded, never raises."""
    def sub(match: re.Match) -> str:
        value = variables.get(match.group(1))
        return "" if value is None else str(value)

    return _TEMPLATE_KEY.sub(sub, template or "")


def _body_for_history(row) -> str:
    body = row["body"] or ""
    if body:
        return body
    media = db.jloads(row["meta"], {}).get("mediaType") if row["meta"] else None
    return f"[{media}]" if media else ""


def _history_lines(conn, conversation_id: int, count: int, tz: ZoneInfo) -> str:
    if count <= 0:
        return ""
    rows = conn.execute(
        "SELECT author, body, ts, meta FROM messages WHERE conversation_id = ? AND status NOT IN ('draft','discarded')"
        " ORDER BY ts DESC, id DESC LIMIT ?",
        (conversation_id, count),
    ).fetchall()
    lines = []
    for row in reversed(rows):
        stamp = datetime.fromtimestamp(row["ts"], tz).strftime("%Y-%m-%d %H:%M")
        lines.append(f"[{stamp}] {row['author']}: {_body_for_history(row)}")
    return "\n".join(lines)


def _template_vars(conn, cfg: dict[str, Any], conv: dict[str, Any], text: str) -> dict[str, Any]:
    account = conn.execute("SELECT label FROM accounts WHERE id = ?", (conv["account_id"],)).fetchone()
    tz = _zone((cfg.get("hours") or {}).get("timezone"))
    return {
        "contact_name": conv.get("contact_name") or conv.get("phone") or "",
        "phone": conv.get("phone") or "",
        "text": text,
        "account_label": account["label"] if account else "",
        "state": conv["state"],
        "conversation_id": conv["id"],
        "history": _history_lines(conn, conv["id"], int(cfg["automations"]["history_messages_in_prompt"]), tz),
        "wa_cli": wa_cli_command(),
    }


# --- External boundaries (seams for tests) ----------------------------------------


def _run_command(
    argv: list[str], *, stdin: str | None = None, timeout: float, env: dict[str, str] | None = None
) -> tuple[int, str, str]:
    """Run a subprocess without a shell; returns (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            argv,
            input=stdin,
            stdin=None if stdin is not None else subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {timeout:g}s") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot run {argv[0]}: {exc}") from exc
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _http_post(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, str]:
    """POST ``body``; returns (status, text) for any HTTP status, raises RuntimeError when unreachable."""
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:  # noqa: S310 - user-configured URL
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeError(f"webhook unreachable: {getattr(exc, 'reason', exc)}") from exc


@functools.lru_cache(maxsize=1)
def _hermes_bin() -> str:
    override = os.environ.get("WA_HERMES_BIN")
    if override:
        return override
    found = shutil.which("hermes")
    if found:
        return found
    home = Path.home()
    for candidate in (
        home / ".local" / "bin" / "hermes",
        Path("/opt/homebrew/bin/hermes"),
        Path("/usr/local/bin/hermes"),
        home / ".hermes" / "hermes-agent" / "venv" / "bin" / "hermes",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return "hermes"


# --- Actions -----------------------------------------------------------------------


def _clean_reply(stdout: str) -> str:
    """Drop only ``session_id: ...`` lines (Hermes prints them around the reply) and trim."""
    kept = [line for line in stdout.splitlines() if not _SESSION_LINE.match(line)]
    return "\n".join(kept).strip()


def _author_for(action: dict[str, Any], profile: str | None, rule_id: int) -> str:
    if action.get("type") == "hermes":
        return f"agent:{profile or 'default'}"
    return f"rule:{rule_id}"


def _deliver(conn, conv_id: int, text: str, *, reply_mode: str, author: str, rule_id: int, now: int) -> str:
    if reply_mode == "none":
        return "reply_mode none: reply stored in run output only"
    if reply_mode == "send":
        message = outbound.send_text(conn, conv_id, text, author=author, now=now, rule_id=rule_id)
        return f"sent as message #{message.get('id')}"
    message = outbound.create_draft(conn, conv_id, text, author=author, now=now, rule_id=rule_id)
    return f"draft #{message.get('id')} created"


def _set_state(conn, conv_id: int, to: str, *, actor: str, reason: str, now: int) -> str | None:
    if to not in STATES:
        raise _Permanent(f"unknown state {to!r}")
    with db.write_txn(conn):
        row = conn.execute("SELECT account_id, state, muted_until FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if row is None:
            raise errors.NotFound(f"conversation {conv_id} not found")
        old = row["state"]
        if old == to:
            return None
        conn.execute(
            "UPDATE conversations SET state = ?, previous_state = ?, muted_until = CASE WHEN ? = 'muted' THEN muted_until ELSE NULL END,"
            " updated_at = ? WHERE id = ?",
            (to, old, to, now, conv_id),
        )
        conn.execute(
            "INSERT INTO conversation_state_log (conversation_id, from_state, to_state, actor, reason, at) VALUES (?,?,?,?,?,?)",
            (conv_id, old, to, actor, reason, now),
        )
        events.emit(
            conn,
            "conversation.state_changed",
            now=now,
            account_id=row["account_id"],
            conversation_id=conv_id,
            payload={"from": old, "to": to, "actor": actor, "reason": reason},
        )
    return f"state {old} -> {to}"


def _add_tags(conn, conv_id: int, tags: list[Any], *, now: int) -> str | None:
    wanted = [t.strip() for t in tags if isinstance(t, str) and t.strip()]
    if not wanted:
        return None
    with db.write_txn(conn):
        row = conn.execute("SELECT account_id, tags FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if row is None:
            raise errors.NotFound(f"conversation {conv_id} not found")
        have = [t for t in db.jloads(row["tags"], []) if isinstance(t, str)]
        added = [t for t in dict.fromkeys(wanted) if t not in have]
        if not added:
            return None
        conn.execute(
            "UPDATE conversations SET tags = ?, updated_at = ? WHERE id = ?",
            (json.dumps(have + added, ensure_ascii=False), now, conv_id),
        )
        events.emit(
            conn, "conversation.updated", now=now, account_id=row["account_id"], conversation_id=conv_id, payload={"fields": ["tags"]}
        )
    return f"tags added: {', '.join(added)}"


def _set_priority(conn, conv_id: int, priority: int, *, now: int) -> str | None:
    with db.write_txn(conn):
        row = conn.execute("SELECT account_id, priority FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if row is None:
            raise errors.NotFound(f"conversation {conv_id} not found")
        if row["priority"] == priority:
            return None
        conn.execute("UPDATE conversations SET priority = ?, updated_at = ? WHERE id = ?", (priority, now, conv_id))
        events.emit(
            conn, "conversation.updated", now=now, account_id=row["account_id"], conversation_id=conv_id, payload={"fields": ["priority"]}
        )
    return "escalated" if priority else "de-escalated"


def _takeover(conn, conv_id: int, *, actor: str, now: int) -> str:
    parts = []
    state = _set_state(conn, conv_id, "in_progress", actor=actor, reason="taken over by automation", now=now)
    if state:
        parts.append(state)
    with db.write_txn(conn):
        row = conn.execute("SELECT account_id, agent_active FROM conversations WHERE id = ?", (conv_id,)).fetchone()
        if row is not None and row["agent_active"]:
            conn.execute("UPDATE conversations SET agent_active = 0, updated_at = ? WHERE id = ?", (now, conv_id))
            events.emit(
                conn,
                "conversation.updated",
                now=now,
                account_id=row["account_id"],
                conversation_id=conv_id,
                payload={"fields": ["agent_active"]},
            )
            parts.append("agent handed off")
    return "; ".join(parts) or "already taken over"


def _directives(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _apply_directives(conn, rule: dict[str, Any], conv_id: int, data: dict[str, Any], now: int) -> list[str]:
    """Apply ``{reply?, state?, tags?, escalate?}`` returned by a webhook or script."""
    parts: list[str] = []
    reply = data.get("reply")
    if isinstance(reply, str) and reply.strip():
        author = _author_for(rule["action"], None, rule["id"])
        parts.append(
            f"reply: {reply.strip()}\n[{_deliver(conn, conv_id, reply.strip(), reply_mode=rule['reply_mode'], author=author, rule_id=rule['id'], now=now)}]"
        )
    if isinstance(data.get("tags"), list):
        done = _add_tags(conn, conv_id, data["tags"], now=now)
        if done:
            parts.append(done)
    if isinstance(data.get("state"), str):
        done = _set_state(
            conn, conv_id, data["state"], actor=f"rule:{rule['id']}", reason=f"automation rule {rule['name']}", now=now
        )
        if done:
            parts.append(done)
    if isinstance(data.get("escalate"), bool):
        done = _set_priority(conn, conv_id, 2 if data["escalate"] else 0, now=now)
        if done:
            parts.append(done)
    return parts


def _payload(conn, cfg: dict[str, Any], run, event, conv: dict[str, Any]) -> bytes:
    count = max(int(cfg["automations"]["history_messages_in_prompt"]), 1)
    rows = conn.execute(
        "SELECT id, direction, author, body, ts, status, source FROM messages WHERE conversation_id = ?"
        " AND status NOT IN ('draft','discarded') ORDER BY ts DESC, id DESC LIMIT ?",
        (conv["id"], count),
    ).fetchall()
    account = conn.execute("SELECT label FROM accounts WHERE id = ?", (conv["account_id"],)).fetchone()
    body = {
        "event": {
            "id": event["id"],
            "type": event["type"],
            "message_id": event["message_id"],
            "at": event["at"],
            "payload": db.jloads(event["payload"], {}),
            "run_id": run["id"],
            "rule_id": run["rule_id"],
        },
        "conversation": {
            "id": conv["id"],
            "account_id": conv["account_id"],
            "account_label": account["label"] if account else None,
            "chat_jid": conv["chat_jid"],
            "contact_name": conv["contact_name"],
            "phone": conv["phone"],
            "state": conv["state"],
            "priority": conv["priority"],
            "agent_active": bool(conv["agent_active"]),
            "tags": conv["tags"],
        },
        "messages": [dict(r) for r in reversed(rows)],
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


def _perform(conn, cfg: dict[str, Any], rule: dict[str, Any], run, event, conv: dict[str, Any], now: int) -> str:
    action = rule["action"]
    kind = action.get("type")
    conv_id = conv["id"]
    actor = f"rule:{rule['id']}"
    reason = f"automation rule {rule['name']}"

    if kind in REPLY_ACTIONS and not conv["agent_active"]:
        raise _Skip("agent_active is off for this conversation (taken over)")

    msg = None
    if event["message_id"] is not None:
        msg = conn.execute("SELECT * FROM messages WHERE id = ?", (event["message_id"],)).fetchone()
    ctx = _context(conn, event["type"], conv, msg, db.jloads(event["payload"], {}))
    variables = _template_vars(conn, cfg, conv, ctx["text"])

    if kind == "hermes":
        account = conn.execute("SELECT hermes_profile FROM accounts WHERE id = ?", (conv["account_id"],)).fetchone()
        profile = action.get("profile") or (account["hermes_profile"] if account else None) or None
        prompt = render_template(action.get("prompt", ""), variables)
        if prompt.startswith("-"):
            prompt = " " + prompt  # argparse would read a leading dash as a flag
        argv = [_hermes_bin()]
        if profile:
            argv += ["-p", profile]
        argv += ["chat", "-Q", "-q", prompt, "--source", "whatsapp-chat"]
        for skill in action.get("skills") or []:
            argv += ["-s", skill]
        code, out, err = _run_command(argv, timeout=float(action.get("timeout_s") or 180))
        if code != 0:
            raise RuntimeError(f"hermes exited with {code}: {(err or out).strip()[-500:]}")
        reply = _clean_reply(out)
        if not reply:
            raise RuntimeError("hermes returned an empty reply")
        outcome = _deliver(
            conn, conv_id, reply, reply_mode=rule["reply_mode"], author=_author_for(action, profile, rule["id"]), rule_id=rule["id"], now=now
        )
        return f"{reply}\n[{outcome}]"

    if kind == "reply":
        text = render_template(action.get("text", ""), variables).strip()
        if not text:
            raise _Permanent("reply text renders empty")
        outcome = _deliver(conn, conv_id, text, reply_mode=rule["reply_mode"], author=f"rule:{rule['id']}", rule_id=rule["id"], now=now)
        return f"{text}\n[{outcome}]"

    if kind in ("webhook", "script"):
        event_row = event
        body = _payload(conn, cfg, run, event_row, conv)
        if kind == "webhook":
            headers = {"Content-Type": "application/json", "User-Agent": "hermes-whatsapp-chat"}
            if action.get("secret"):
                digest = hmac.new(action["secret"].encode("utf-8"), body, hashlib.sha256).hexdigest()
                headers["X-WA-Signature"] = f"sha256={digest}"
            status, text = _http_post(action["url"], body, headers, float(action.get("timeout_s") or 30))
            if not 200 <= status < 300:
                raise RuntimeError(f"webhook returned HTTP {status}: {text.strip()[:300]}")
        else:
            command = [str(a) for a in action.get("command") or []]
            if not command:
                raise _Permanent("script command is empty")
            code, text, err = _run_command(command, stdin=body.decode("utf-8"), timeout=float(action.get("timeout_s") or 60))
            if code != 0:
                raise RuntimeError(f"script exited with {code}: {(err or text).strip()[-500:]}")
        data = _directives(text)
        if data is None:
            return f"{kind} responded without a JSON object; nothing applied"
        parts = _apply_directives(conn, rule, conv_id, data, now)
        return "\n".join(parts) or f"{kind} ok; nothing to apply"

    if kind == "set_state":
        return _set_state(conn, conv_id, str(action.get("state")), actor=actor, reason=reason, now=now) or "state unchanged"
    if kind == "add_tags":
        return _add_tags(conn, conv_id, action.get("tags") or [], now=now) or "no new tags"
    if kind == "escalate":
        return _set_priority(conn, conv_id, 2, now=now) or "already escalated"
    if kind == "takeover":
        return _takeover(conn, conv_id, actor=actor, now=now)
    raise _Permanent(f"unknown action type {kind!r}")


# --- Queue --------------------------------------------------------------------------


def _stale_after(action_json: str | None) -> int:
    """Seconds a run may stay `running`: the action timeout + 60 s, 300 s when unknown."""
    timeout = db.jloads(action_json, {}).get("timeout_s") if action_json else None
    if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
        return int(timeout) + 60
    return DEFAULT_STALE_SECONDS


def _recover_stale(conn, now: int) -> None:
    """Runs left `running` by a killed process go back to the queue, or fail after MAX_ATTEMPTS."""
    rows = conn.execute(
        "SELECT r.id, r.rule_id, r.conversation_id, r.attempts, r.started_at, u.action AS action, c.account_id AS account_id"
        " FROM automation_runs r LEFT JOIN automation_rules u ON u.id = r.rule_id"
        " LEFT JOIN conversations c ON c.id = r.conversation_id WHERE r.status = 'running'"
    ).fetchall()
    for row in rows:
        if row["started_at"] is None or now - row["started_at"] <= _stale_after(row["action"]):
            continue
        if row["attempts"] < MAX_ATTEMPTS:
            conn.execute(
                "UPDATE automation_runs SET status = 'queued', not_before = NULL, error = 'interrupted while running' WHERE id = ?",
                (row["id"],),
            )
            continue
        conn.execute(
            "UPDATE automation_runs SET status = 'failed', error = 'interrupted while running', finished_at = ? WHERE id = ?",
            (now, row["id"]),
        )
        events.emit(
            conn,
            "automation.run",
            now=now,
            account_id=row["account_id"],
            conversation_id=row["conversation_id"],
            payload={"rule_id": row["rule_id"], "run_id": row["id"], "status": "failed"},
        )


def _claim(conn, now: int, limit: int) -> list[Any]:
    with db.write_txn(conn):
        _recover_stale(conn, now)
        ids = [
            r["id"]
            for r in conn.execute(
                "SELECT id FROM automation_runs WHERE status = 'queued' AND (not_before IS NULL OR not_before <= ?)"
                " ORDER BY id LIMIT ?",
                (now, limit),
            )
        ]
        for run_id in ids:
            conn.execute(
                "UPDATE automation_runs SET status = 'running', started_at = ?, attempts = attempts + 1 WHERE id = ?",
                (now, run_id),
            )
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return conn.execute(f"SELECT * FROM automation_runs WHERE id IN ({marks}) ORDER BY id", ids).fetchall()


def _finish(
    conn, run, *, status: str, now: int, account_id: int | None, output: str | None = None, error: str | None = None,
    retry_at: int | None = None,
) -> None:
    with db.write_txn(conn):
        if retry_at is not None:
            conn.execute(
                "UPDATE automation_runs SET status = 'queued', not_before = ?, error = ?, output = ? WHERE id = ?",
                (retry_at, error, output, run["id"]),
            )
            return
        conn.execute(
            "UPDATE automation_runs SET status = ?, output = ?, error = ?, finished_at = ? WHERE id = ?",
            (status, output, error, now, run["id"]),
        )
        events.emit(
            conn,
            "automation.run",
            now=now,
            account_id=account_id,
            conversation_id=run["conversation_id"],
            payload={"rule_id": run["rule_id"], "run_id": run["id"], "status": status},
        )


def _execute(conn, run, now0: int, started: float) -> None:
    def clock() -> int:
        return now0 + int(time.monotonic() - started)

    def done(status: str, account_id: int | None, output: str | None = None, error: str | None = None, retry_at: int | None = None):
        _finish(conn, run, status=status, now=clock(), account_id=account_id, output=output, error=error, retry_at=retry_at)

    rule_row = conn.execute("SELECT * FROM automation_rules WHERE id = ?", (run["rule_id"],)).fetchone()
    conv = _conversation(conn, run["conversation_id"])
    account_id = conv["account_id"] if conv else None
    if rule_row is None:
        return done("skipped", account_id, error="rule deleted")
    if conv is None:
        return done("skipped", account_id, error="conversation not found")
    event = conn.execute("SELECT * FROM events WHERE id = ?", (run["event_id"],)).fetchone()
    if event is None:
        return done("skipped", account_id, error="event not found")
    rule = rule_to_dict(rule_row)
    try:
        output = _perform(conn, _cfg(conn), rule, run, event, conv, clock())
    except _Skip as skip:
        return done("skipped", account_id, error=str(skip))
    except (_Permanent, errors.Invalid, errors.NotFound) as exc:
        return done("failed", account_id, error=str(exc) or type(exc).__name__)
    except Exception as exc:  # noqa: BLE001 - any executor failure is recorded on the run
        message = str(exc) or type(exc).__name__
        attempts = int(run["attempts"])
        if attempts < MAX_ATTEMPTS:
            return done("queued", account_id, error=message, retry_at=clock() + BACKOFF_SECONDS[attempts - 1])
        return done("failed", account_id, error=message)
    return done("done", account_id, output=output)


def process_due(connect_fn: Callable[[], Any], now: int, *, limit: int = 4) -> int:
    """Claim queued runs that are due, execute them outside any transaction, record outcomes."""
    conn = connect_fn()
    try:
        runs = _claim(conn, now, limit)
        for run in runs:
            started = time.monotonic()
            try:
                _execute(conn, run, now, started)
            except Exception:
                log.exception("automation run %s could not be recorded", run["id"])
        return len(runs)
    finally:
        conn.close()
