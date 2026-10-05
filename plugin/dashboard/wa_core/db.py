"""Paths, schema v6, v1/v2/v3/v4/v5 -> v6 migrations, connection helpers."""

from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

PLUGIN_ID = "hermes-whatsapp-chat"
SCHEMA_VERSION = 6
DEMO_SUFFIX = "@demo.invalid"

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS accounts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  kind TEXT NOT NULL DEFAULT 'whatsapp' CHECK (kind IN ('whatsapp','demo')),
  label TEXT NOT NULL, color TEXT NOT NULL DEFAULT 'blue',
  phone TEXT, wa_name TEXT, wa_jid TEXT,
  port INTEGER UNIQUE, session_dir TEXT UNIQUE,
  desired TEXT NOT NULL DEFAULT 'running' CHECK (desired IN ('running','stopped','logged_out','removed')),
  history_mode TEXT NOT NULL DEFAULT 'recent' CHECK (history_mode IN ('off','recent','full')),
  hermes_profile TEXT, restart_requested_at INTEGER,
  delete_conversations INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, updated_at INTEGER,
  new_contact_list TEXT NOT NULL DEFAULT 'unclassified', new_group_list TEXT NOT NULL DEFAULT 'unclassified');
CREATE TABLE IF NOT EXISTS account_status (
  account_id INTEGER PRIMARY KEY, state TEXT NOT NULL,
  qr TEXT, qr_svg TEXT, qr_at INTEGER, error TEXT, pid INTEGER,
  heartbeat_at INTEGER, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS service_status (
  id INTEGER PRIMARY KEY CHECK (id = 1), pid INTEGER, version TEXT,
  started_at INTEGER, heartbeat_at INTEGER);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at INTEGER);
CREATE TABLE IF NOT EXISTS conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT, account_id INTEGER NOT NULL, chat_jid TEXT NOT NULL,
  contact_name TEXT, phone TEXT,
  state TEXT NOT NULL DEFAULT 'new', previous_state TEXT,
  priority INTEGER NOT NULL DEFAULT 0, muted_until INTEGER,
  last_message_at INTEGER, last_inbound_at INTEGER,
  unread_count INTEGER NOT NULL DEFAULT 0, agent_active INTEGER NOT NULL DEFAULT 1,
  tags TEXT NOT NULL DEFAULT '[]', created_at INTEGER, updated_at INTEGER, classification TEXT,
  is_group INTEGER NOT NULL DEFAULT 0, list_override TEXT, silenced_until INTEGER, participants TEXT,
  group_refreshed_at INTEGER,
  UNIQUE (account_id, chat_jid));
CREATE TABLE IF NOT EXISTS messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
  wa_id TEXT, direction TEXT NOT NULL CHECK (direction IN ('in','out')),
  author TEXT NOT NULL, body TEXT, ts INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN
    ('received','pending','sent','delivered','read','played','failed','draft','discarded')),
  source TEXT NOT NULL DEFAULT 'live' CHECK (source IN ('live','history')),
  meta TEXT, error TEXT, rule_id INTEGER,
  delivered_at INTEGER, read_at INTEGER, remote_jid TEXT, sender_jid TEXT, sender_name TEXT, participant TEXT);
CREATE TABLE IF NOT EXISTS conversation_state_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
  from_state TEXT, to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, type TEXT NOT NULL,
  account_id INTEGER, conversation_id INTEGER, message_id INTEGER,
  payload TEXT, at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS automation_rules (
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
  position INTEGER NOT NULL DEFAULT 0, account_id INTEGER,
  event_types TEXT NOT NULL DEFAULT '["message.in"]', conditions TEXT NOT NULL DEFAULT '{}',
  action TEXT NOT NULL, reply_mode TEXT NOT NULL DEFAULT 'draft' CHECK (reply_mode IN ('draft','send','none')),
  stop_after_match INTEGER NOT NULL DEFAULT 0,
  created_at INTEGER NOT NULL, updated_at INTEGER, managed_by TEXT);
CREATE TABLE IF NOT EXISTS automation_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, rule_id INTEGER NOT NULL, event_id INTEGER NOT NULL,
  conversation_id INTEGER, status TEXT NOT NULL CHECK (status IN ('queued','running','done','failed','skipped')),
  attempts INTEGER NOT NULL DEFAULT 0, not_before INTEGER, output TEXT, error TEXT,
  created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER);
CREATE TABLE IF NOT EXISTS jev_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
  message_id INTEGER, status TEXT NOT NULL CHECK (status IN ('queued','running','done','failed','skipped')),
  attempts INTEGER NOT NULL DEFAULT 0, not_before INTEGER, error TEXT, model TEXT,
  latency_ms INTEGER, input_tokens INTEGER,
  created_at INTEGER NOT NULL, started_at INTEGER, finished_at INTEGER);
CREATE TABLE IF NOT EXISTS contact_lists (
  jid TEXT PRIMARY KEY,
  list TEXT NOT NULL CHECK (list IN ('admin','work','personal','unclassified','ignored')),
  updated_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS idx_messages_conv_ts ON messages(conversation_id, ts, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_wa_id ON messages(account_id, wa_id) WHERE wa_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conversations_state ON conversations(state);
CREATE INDEX IF NOT EXISTS idx_conversations_last ON conversations(last_message_at);
CREATE INDEX IF NOT EXISTS idx_state_log_conv ON conversation_state_log(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_events_conv ON events(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_runs_status ON automation_runs(status, not_before);
CREATE INDEX IF NOT EXISTS idx_jev_runs_status ON jev_runs(status, not_before);
CREATE INDEX IF NOT EXISTS idx_jev_runs_conv ON jev_runs(conversation_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_drafts ON messages(conversation_id) WHERE status = 'draft';
"""

_V1_TABLES = ("conversations", "messages", "conversation_state_log")
_V1_INDEXES = (
    "idx_messages_chat_ts",
    "idx_messages_wa_id",
    "idx_conversations_state",
    "idx_state_log_chat",
)
_V2_MESSAGE_INDEXES = (
    "idx_messages_conv_ts",
    "idx_messages_wa_id",
    "idx_messages_drafts",
)


# --- Paths ---------------------------------------------------------------------


def hermes_home(platform: str | None = None) -> Path:
    """``HERMES_HOME``, else Hermes' platform default: ``%LOCALAPPDATA%\\hermes`` on Windows, ``~/.hermes``
    elsewhere (mirrors ``hermes_constants``; ``platform`` defaults to ``sys.platform``)."""
    configured = os.environ.get("HERMES_HOME")
    if configured:
        return Path(configured)
    if (platform or sys.platform) == "win32":
        local = os.environ.get("LOCALAPPDATA", "").strip()
        return (Path(local) if local else Path.home() / "AppData" / "Local") / "hermes"
    return Path.home() / ".hermes"


def data_dir() -> Path:
    """Durable data root: ``<hermes home>/plugin-data/hermes-whatsapp-chat/``."""
    try:
        from plugins.plugin_storage import plugin_data_dir  # ty: ignore[unresolved-import]

        return Path(plugin_data_dir(PLUGIN_ID))
    except ImportError:
        # Dev venv, sidecar, seed script: same layout, computed locally.
        root = hermes_home() / "plugin-data" / PLUGIN_ID
        root.mkdir(parents=True, exist_ok=True)
        return root


def db_path() -> Path:
    raw = os.environ.get("WA_ARCHIVE_DB")
    return Path(raw).expanduser() if raw else data_dir() / "wa_board.db"


# --- Helpers -------------------------------------------------------------------


def jloads(raw: Any, default: Any) -> Any:
    """Tolerant ``json.loads``: any malformed or missing input yields ``default``."""
    if raw is None or raw == "":
        return default
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return default


@contextmanager
def write_txn(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


# --- Connect + migrate ---------------------------------------------------------


def _statements() -> list[str]:
    return [
        s.strip()
        for s in SCHEMA.split(";")
        if s.strip() and not s.strip().startswith("PRAGMA")
    ]


def _is_v1(conn: sqlite3.Connection) -> bool:
    cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)")]
    return "chat_jid" in cols and "account_id" not in cols


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        if conn.execute("PRAGMA user_version").fetchone()[0] < SCHEMA_VERSION:
            _upgrade(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def _upgrade(conn: sqlite3.Connection) -> None:
    with write_txn(conn):
        # Re-check inside the lock: another process may have migrated while we waited.
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version >= SCHEMA_VERSION:
            return
        if _is_v1(conn):
            _migrate_v1(conn, int(time.time()))
            _migrate_v5(conn)
        elif version == 2:
            _migrate_v2(conn)
            _migrate_v3(conn)
            _migrate_v4(conn)
            _migrate_v5(conn)
        elif version == 3:
            _migrate_v3(conn)
            _migrate_v4(conn)
            _migrate_v5(conn)
        elif version == 4:
            _migrate_v4(conn)
            _migrate_v5(conn)
        elif version == 5:
            _migrate_v5(conn)
        else:
            for stmt in _statements():
                conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")


def _migrate_v3(conn: sqlite3.Connection) -> None:
    """v3 -> v4: ``conversations.classification`` and the ``jev_runs`` table."""
    if "classification" not in [
        r[1] for r in conn.execute("PRAGMA table_info(conversations)")
    ]:
        conn.execute("ALTER TABLE conversations ADD COLUMN classification TEXT")
    for stmt in _statements():
        conn.execute(stmt)


def _add_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
    have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
    for name, ddl in columns.items():
        if name not in have:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


def _migrate_v5(conn: sqlite3.Connection) -> None:
    """v5 -> v6: groups as conversations, contact lists, silence, per-message sender, per-number list defaults.

    Idempotent (it also finishes a v1 migration): every existing conversation's chat JID starts in ``unclassified``.
    """
    _add_columns(
        conn,
        "conversations",
        {
            "is_group": "INTEGER NOT NULL DEFAULT 0",
            "list_override": "TEXT",
            "silenced_until": "INTEGER",
            "participants": "TEXT",
            "group_refreshed_at": "INTEGER",
        },
    )
    _add_columns(
        conn,
        "messages",
        {"sender_jid": "TEXT", "sender_name": "TEXT", "participant": "TEXT"},
    )
    _add_columns(
        conn,
        "accounts",
        {
            "new_contact_list": "TEXT NOT NULL DEFAULT 'unclassified'",
            "new_group_list": "TEXT NOT NULL DEFAULT 'unclassified'",
        },
    )
    for stmt in _statements():
        conn.execute(stmt)
    conn.execute("UPDATE conversations SET is_group = 1 WHERE chat_jid LIKE '%@g.us'")
    conn.execute(
        "INSERT OR IGNORE INTO contact_lists (jid, list, updated_at)"
        " SELECT DISTINCT chat_jid, 'unclassified', ? FROM conversations",
        (int(time.time()),),
    )


def _migrate_v4(conn: sqlite3.Connection) -> None:
    """v4 -> v5: ``automation_rules.managed_by`` (NULL = user rule, ``'jev'`` = generated from the Jev rules)."""
    if "managed_by" not in [
        r[1] for r in conn.execute("PRAGMA table_info(automation_rules)")
    ]:
        conn.execute("ALTER TABLE automation_rules ADD COLUMN managed_by TEXT")


def _migrate_v2(conn: sqlite3.Connection) -> None:
    """v2 -> v3: rebuild ``messages`` (SQLite cannot alter a CHECK) with receipt columns."""
    for idx in _V2_MESSAGE_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {idx}")
    conn.execute("ALTER TABLE messages RENAME TO v2_messages")
    for stmt in _statements():
        conn.execute(stmt)
    # Old inbound rows count as read, so the first open after the upgrade sends no receipts for them.
    conn.execute(
        "INSERT INTO messages (id, conversation_id, account_id, wa_id, direction, author, body, ts, status,"
        " source, meta, error, rule_id, read_at)"
        " SELECT id, conversation_id, account_id, wa_id, direction, author, body, ts, status,"
        " source, meta, error, rule_id, CASE WHEN direction = 'in' THEN ts END FROM v2_messages"
    )
    conn.execute("DROP TABLE v2_messages")


def _migrate_v1(conn: sqlite3.Connection, now: int) -> None:
    from . import media  # lazy: media imports this module

    for idx in _V1_INDEXES:
        conn.execute(f"DROP INDEX IF EXISTS {idx}")
    for table in _V1_TABLES:
        conn.execute(f"ALTER TABLE {table} RENAME TO v1_{table}")
    for stmt in _statements():
        conn.execute(stmt)

    pattern = f"%{DEMO_SUFFIX}"
    has_real = conn.execute(
        "SELECT 1 FROM v1_conversations WHERE chat_jid NOT LIKE ? LIMIT 1", (pattern,)
    ).fetchone()
    has_demo = conn.execute(
        "SELECT 1 FROM v1_conversations WHERE chat_jid LIKE ? LIMIT 1", (pattern,)
    ).fetchone()
    main_id = demo_id = None
    if has_real or (data_dir() / "wa-session" / "creds.json").exists():
        main_id = conn.execute(
            "INSERT INTO accounts (kind, label, color, port, session_dir, created_at, updated_at)"
            " VALUES ('whatsapp','Main','green',3017,'wa-session',?,?)",
            (now, now),
        ).lastrowid
    if has_demo:
        demo_id = conn.execute(
            "INSERT INTO accounts (kind, label, color, created_at, updated_at) VALUES ('demo','Demo','gray',?,?)",
            (now, now),
        ).lastrowid

    conn.execute(
        "INSERT INTO conversations (account_id, chat_jid, contact_name, phone, state, priority, muted_until,"
        " last_message_at, last_inbound_at, unread_count, agent_active, tags, created_at, updated_at)"
        " SELECT CASE WHEN c.chat_jid LIKE ? THEN ? ELSE ? END, c.chat_jid, c.contact_name, c.phone, c.state,"
        " c.priority, c.muted_until, c.last_message_at,"
        " (SELECT MAX(m.ts) FROM v1_messages m WHERE m.chat_jid = c.chat_jid AND m.direction = 'in'),"
        " c.unread_count, c.agent_active, c.tags,"
        " COALESCE((SELECT MIN(m.ts) FROM v1_messages m WHERE m.chat_jid = c.chat_jid), c.updated_at, ?),"
        " COALESCE(c.updated_at, ?) FROM v1_conversations c ORDER BY c.rowid",
        (pattern, demo_id, main_id, now, now),
    )
    conn.execute(
        "INSERT INTO messages (id, conversation_id, account_id, wa_id, direction, author, body, ts, status, source,"
        " meta, read_at)"
        " SELECT m.id, c.id, c.account_id, m.wa_id, m.direction,"
        " CASE m.direction WHEN 'in' THEN 'contact' ELSE 'user' END, m.body, m.ts,"
        " CASE m.direction WHEN 'in' THEN 'received' ELSE 'sent' END, 'live', m.meta,"
        " CASE WHEN m.direction = 'in' THEN m.ts END"
        " FROM v1_messages m JOIN conversations c ON c.chat_jid = m.chat_jid"
    )
    conn.execute(
        "INSERT INTO conversation_state_log (id, conversation_id, from_state, to_state, actor, reason, at)"
        " SELECT l.id, c.id, l.from_state, l.to_state, l.actor, l.reason, l.at"
        " FROM v1_conversation_state_log l JOIN conversations c ON c.chat_jid = l.chat_jid"
    )
    for row in conn.execute(
        "SELECT id, meta FROM messages WHERE meta IS NOT NULL"
    ).fetchall():
        meta = jloads(row["meta"], None)
        if not isinstance(meta, dict) or "mediaUrls" not in meta:
            continue
        new_meta = {
            "mediaType": meta.get("mediaType"),
            "media": media.media_entries(meta.get("mediaUrls")),
        }
        conn.execute(
            "UPDATE messages SET meta = ? WHERE id = ?",
            (json.dumps(new_meta), row["id"]),
        )
    for table in _V1_TABLES:
        conn.execute(f"DROP TABLE v1_{table}")
