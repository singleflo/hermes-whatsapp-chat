"""WhatsApp accounts (one bridge + session each), their reported status, and the service heartbeat."""

from __future__ import annotations

import shutil
import sqlite3
from pathlib import Path
from typing import Any

from . import db, errors, service

HISTORY_MODES = ("off", "recent", "full")
SETTABLE_DESIRED = ("running", "stopped", "logged_out")
FIRST_PORT = 3017
RESERVED_PORTS = {3000}
HEARTBEAT_FRESH_SECONDS = 10
MAX_LABEL = 60
MAX_COLOR = 24


def _emit(conn: sqlite3.Connection, account_id: int, now: int) -> None:
    from . import events  # lazy: events pulls in the automations slice, which imports this module

    row = conn.execute("SELECT state FROM account_status WHERE account_id = ?", (account_id,)).fetchone()
    events.emit(
        conn, "account.status", now=now, account_id=account_id, payload={"state": row["state"] if row else "stopped"}
    )


# --- Paths -----------------------------------------------------------------------


def session_path(account: Any) -> Path:
    session_dir = account["session_dir"]
    if not session_dir:
        raise errors.Invalid(f"account {account['id']} has no WhatsApp session")
    return db.data_dir() / session_dir


def media_dir(account: Any) -> Path:
    return db.data_dir() / "wa-media" / str(account["id"])


def is_paired(account: Any) -> bool:
    if not account["session_dir"]:
        return False
    return (session_path(account) / "creds.json").exists()


# --- Reading ---------------------------------------------------------------------


def _status_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "state": row["state"],
        "qr_svg": row["qr_svg"],
        "qr_at": row["qr_at"],
        "error": row["error"],
        "pid": row["pid"],
        "heartbeat_at": row["heartbeat_at"],
        "updated_at": row["updated_at"],
    }


def _account_dict(row: sqlite3.Row, status: sqlite3.Row | None) -> dict[str, Any]:
    return {
        "id": row["id"],
        "kind": row["kind"],
        "label": row["label"],
        "color": row["color"],
        "phone": row["phone"],
        "wa_name": row["wa_name"],
        "wa_jid": row["wa_jid"],
        "port": row["port"],
        "session_dir": row["session_dir"],
        "desired": row["desired"],
        "history_mode": row["history_mode"],
        "hermes_profile": row["hermes_profile"],
        "restart_requested_at": row["restart_requested_at"],
        "delete_conversations": bool(row["delete_conversations"]),
        "paired": is_paired(row),
        "status": _status_dict(status) if row["kind"] == "whatsapp" else None,
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def list_accounts(conn: sqlite3.Connection, *, include_removed: bool = False) -> list[dict[str, Any]]:
    statuses = {r["account_id"]: r for r in conn.execute("SELECT * FROM account_status")}
    where = "" if include_removed else " WHERE desired != 'removed'"
    rows = conn.execute(f"SELECT * FROM accounts{where} ORDER BY id").fetchall()
    return [_account_dict(r, statuses.get(r["id"])) for r in rows]


def get_account(conn: sqlite3.Connection, account_id: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
    if row is None:
        raise errors.NotFound(f"account {account_id} not found")
    status = conn.execute("SELECT * FROM account_status WHERE account_id = ?", (account_id,)).fetchone()
    return _account_dict(row, status)


# --- Mutations -------------------------------------------------------------------


def _clean_label(label: Any) -> str:
    text = str(label or "").strip()
    if not text:
        raise errors.Invalid("account label is empty")
    if len(text) > MAX_LABEL:
        raise errors.Invalid(f"account label is longer than {MAX_LABEL} characters")
    return text


def _clean_color(color: Any) -> str:
    text = str(color or "").strip()
    if not text or len(text) > MAX_COLOR:
        raise errors.Invalid(f"account color must be 1-{MAX_COLOR} characters")
    return text


def _clean_history_mode(mode: Any) -> str:
    if mode not in HISTORY_MODES:
        raise errors.Invalid(f"history_mode must be one of {list(HISTORY_MODES)}")
    return mode


def _next_port(conn: sqlite3.Connection) -> int:
    used = {r[0] for r in conn.execute("SELECT port FROM accounts WHERE port IS NOT NULL")} | RESERVED_PORTS
    port = FIRST_PORT
    while port in used:
        port += 1
    return port


def create_account(
    conn: sqlite3.Connection, *, label: str, color: str = "blue", history_mode: str = "recent", now: int
) -> dict[str, Any]:
    label, color, history_mode = _clean_label(label), _clean_color(color), _clean_history_mode(history_mode)
    from . import events  # lazy, see _emit

    with db.write_txn(conn):
        cur = conn.execute(
            "INSERT INTO accounts (kind, label, color, port, history_mode, created_at, updated_at)"
            " VALUES ('whatsapp',?,?,?,?,?,?)",
            (label, color, _next_port(conn), history_mode, now, now),
        )
        account_id = cur.lastrowid or 0
        conn.execute("UPDATE accounts SET session_dir = ? WHERE id = ?", (f"sessions/{account_id}", account_id))
        conn.execute(
            "INSERT INTO account_status (account_id, state, updated_at) VALUES (?, 'starting', ?)", (account_id, now)
        )
        events.emit(conn, "account.status", now=now, account_id=account_id, payload={"state": "starting"})
    return get_account(conn, account_id)


def update_account(conn: sqlite3.Connection, account_id: int, *, now: int, **fields: Any) -> dict[str, Any]:
    unknown = set(fields) - {"label", "color", "history_mode", "hermes_profile"}
    if unknown:
        raise errors.Invalid(f"cannot update fields: {sorted(unknown)}")
    clean: dict[str, Any] = {}
    if "label" in fields:
        clean["label"] = _clean_label(fields["label"])
    if "color" in fields:
        clean["color"] = _clean_color(fields["color"])
    if "history_mode" in fields:
        clean["history_mode"] = _clean_history_mode(fields["history_mode"])
    if "hermes_profile" in fields:
        profile = str(fields["hermes_profile"] or "").strip()
        clean["hermes_profile"] = profile or None
    with db.write_txn(conn):
        _require(conn, account_id)
        if clean:
            sets = ", ".join(f"{k} = ?" for k in clean)
            conn.execute(f"UPDATE accounts SET {sets}, updated_at = ? WHERE id = ?", (*clean.values(), now, account_id))
            _emit(conn, account_id, now)
    return get_account(conn, account_id)


def _require(conn: sqlite3.Connection, account_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM accounts WHERE id = ? AND desired != 'removed'", (account_id,)).fetchone()
    if row is None:
        raise errors.NotFound(f"account {account_id} not found")
    return row


def set_desired(conn: sqlite3.Connection, account_id: int, desired: str, *, now: int) -> dict[str, Any]:
    if desired not in SETTABLE_DESIRED:
        raise errors.Invalid(f"desired must be one of {list(SETTABLE_DESIRED)}")
    with db.write_txn(conn):
        row = _require(conn, account_id)
        if row["kind"] == "demo":
            raise errors.Invalid("the demo account has no WhatsApp connection")
        conn.execute("UPDATE accounts SET desired = ?, updated_at = ? WHERE id = ?", (desired, now, account_id))
        _emit(conn, account_id, now)
    return get_account(conn, account_id)


def request_restart(conn: sqlite3.Connection, account_id: int, *, now: int) -> dict[str, Any]:
    with db.write_txn(conn):
        row = _require(conn, account_id)
        if row["kind"] == "demo":
            raise errors.Invalid("the demo account has no WhatsApp connection")
        conn.execute(
            "UPDATE accounts SET desired = 'running', restart_requested_at = ?, updated_at = ? WHERE id = ?",
            (now, now, account_id),
        )
        _emit(conn, account_id, now)
    return get_account(conn, account_id)


def purge_conversations(conn: sqlite3.Connection, account_id: int) -> None:
    """Delete every conversation of the account with messages, logs, events and runs. Caller holds a txn."""
    ids = "SELECT id FROM conversations WHERE account_id = ?"
    for table in ("messages", "conversation_state_log"):
        conn.execute(f"DELETE FROM {table} WHERE conversation_id IN ({ids})", (account_id,))
    conn.execute(f"DELETE FROM automation_runs WHERE conversation_id IN ({ids})", (account_id,))
    conn.execute(f"DELETE FROM events WHERE conversation_id IN ({ids})", (account_id,))
    conn.execute("DELETE FROM conversations WHERE account_id = ?", (account_id,))


def _drop_account(conn: sqlite3.Connection, account_id: int, delete_conversations: bool) -> None:
    if delete_conversations:
        purge_conversations(conn, account_id)
    conn.execute("DELETE FROM events WHERE account_id = ? AND conversation_id IS NULL", (account_id,))
    conn.execute("DELETE FROM account_status WHERE account_id = ?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id = ?", (account_id,))


def _delete_files(account_id: int) -> None:
    root = db.data_dir()
    for path in (root / "wa-media" / str(account_id), root / "uploads" / str(account_id)):
        shutil.rmtree(path, ignore_errors=True)


def remove_account(conn: sqlite3.Connection, account_id: int, *, delete_conversations: bool, now: int) -> None:
    with db.write_txn(conn):
        row = _require(conn, account_id)
        if row["kind"] == "demo":
            _drop_account(conn, account_id, True)
        else:
            conn.execute(
                "UPDATE accounts SET desired = 'removed', delete_conversations = ?, updated_at = ? WHERE id = ?",
                (1 if delete_conversations else 0, now, account_id),
            )
            _emit(conn, account_id, now)
    if row["kind"] == "demo":
        _delete_files(account_id)


def finalize_removed(conn: sqlite3.Connection, account_id: int) -> None:
    """Sidecar calls this once the bridge is stopped and the session deleted."""
    with db.write_txn(conn):
        row = conn.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        if row is None:
            return
        flagged = bool(row["delete_conversations"])
        _drop_account(conn, account_id, flagged)
    if flagged:
        _delete_files(account_id)


# --- Status reported by the sidecar ------------------------------------------------


def set_status(
    conn: sqlite3.Connection,
    account_id: int,
    *,
    state: str,
    now: int,
    qr: str | None = None,
    qr_svg: str | None = None,
    error: str | None = None,
    pid: int | None = None,
) -> None:
    from . import events  # lazy, see _emit

    with db.write_txn(conn):
        old = conn.execute("SELECT * FROM account_status WHERE account_id = ?", (account_id,)).fetchone()
        qr_at = now if qr is not None else None
        if state == "qr" and qr is None and old is not None:
            qr, qr_svg, qr_at = old["qr"], old["qr_svg"], old["qr_at"]
        if old is not None and qr is not None and qr == old["qr"]:
            qr_at = old["qr_at"]
        conn.execute(
            "INSERT INTO account_status (account_id, state, qr, qr_svg, qr_at, error, pid, heartbeat_at, updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(account_id) DO UPDATE SET state = excluded.state, qr = excluded.qr,"
            " qr_svg = excluded.qr_svg, qr_at = excluded.qr_at, error = excluded.error,"
            " pid = COALESCE(excluded.pid, account_status.pid), heartbeat_at = excluded.heartbeat_at,"
            " updated_at = CASE WHEN account_status.state = excluded.state AND account_status.qr IS excluded.qr"
            " AND account_status.error IS excluded.error THEN account_status.updated_at ELSE excluded.updated_at END",
            (account_id, state, qr, qr_svg, qr_at, error, pid, now, now),
        )
        if old is None or (old["state"], old["qr"], old["error"]) != (state, qr, error):
            events.emit(conn, "account.status", now=now, account_id=account_id, payload={"state": state})


def record_paired(conn: sqlite3.Connection, account_id: int, *, wa_jid: str, wa_name: str | None, now: int) -> None:
    phone = wa_jid.split("@", 1)[0].split(":", 1)[0]
    with db.write_txn(conn):
        _require(conn, account_id)
        conn.execute(
            "UPDATE accounts SET wa_jid = ?, wa_name = ?, phone = ?, updated_at = ? WHERE id = ?",
            (wa_jid, wa_name, phone, now, account_id),
        )
        _emit(conn, account_id, now)


# --- Service heartbeat ---------------------------------------------------------------


def service_heartbeat(conn: sqlite3.Connection, *, pid: int, version: str, started_at: int, now: int) -> None:
    with db.write_txn(conn):
        conn.execute(
            "INSERT INTO service_status (id, pid, version, started_at, heartbeat_at) VALUES (1,?,?,?,?)"
            " ON CONFLICT(id) DO UPDATE SET pid = excluded.pid, version = excluded.version,"
            " started_at = excluded.started_at, heartbeat_at = excluded.heartbeat_at",
            (pid, version, started_at, now),
        )


def service_info(conn: sqlite3.Connection, now: int) -> dict[str, Any]:
    row = conn.execute("SELECT * FROM service_status WHERE id = 1").fetchone()
    heartbeat = row["heartbeat_at"] if row else None
    return {
        "api_version": service.API_VERSION,
        "running": heartbeat is not None and now - heartbeat <= HEARTBEAT_FRESH_SECONDS,
        "pid": row["pid"] if row else None,
        "version": row["version"] if row else None,
        "started_at": row["started_at"] if row else None,
        "heartbeat_at": heartbeat,
        "installed": service.service_installed(),
        "skill_installed": service.skill_installed(),
        "node": service.find_node(),
        "install_command": None,  # kept for compatibility; the UI installs the service itself
    }
