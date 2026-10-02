"""Backend tests for hermes-whatsapp-chat.

Loads the REAL plugin/dashboard/plugin_api.py (which loads the ``wa_core`` package), mounts its
router in a bare FastAPI app and drives it over HTTP against a throwaway SQLite DB. Core
functions are called directly only where the entry point is not HTTP (the sidecar's ingest,
timers, status reports). Only the WhatsApp bridge (an external process) is faked.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sqlite3
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = REPO_ROOT / "plugin" / "dashboard" / "plugin_api.py"
SEED_FILE = REPO_ROOT / "scripts" / "seed_demo.py"
PREFIX = "/api/plugins/hermes-whatsapp-chat"
CONTACT_JID = "393330001111@s.whatsapp.net"
OWN_JID = "393990000000@s.whatsapp.net"
NOW = int(time.time())


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- Fixtures ------------------------------------------------------------------


@pytest.fixture
def plugin(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WA_ARCHIVE_DB", str(tmp_path / "wa_board.db"))
    return _load("hermes_dashboard_plugin_hermes_whatsapp_chat", PLUGIN_FILE)


@pytest.fixture
def core(plugin):
    return plugin.core


@pytest.fixture(autouse=True)
def bridge(plugin, monkeypatch):
    calls: list[tuple] = []
    replies: dict = {}

    def fake(port, method, path, payload=None, *, timeout):
        calls.append((port, method, path, payload))
        if path not in replies:
            raise plugin.core.bridge.BridgeUnavailable("no bridge in tests")
        reply = replies[path]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(plugin.core.bridge, "bridge_request", fake)
    return SimpleNamespace(calls=calls, replies=replies)


@pytest.fixture
def client(plugin):
    app = FastAPI()
    app.include_router(plugin.router, prefix=PREFIX)
    return TestClient(app)


@pytest.fixture
def db(plugin):
    conn = plugin.core.db.connect()
    yield conn
    conn.close()


@pytest.fixture
def account(core, db):
    return add_account(core, db)


@pytest.fixture
def seed_mod():
    return _load("hwc_seed_demo", SEED_FILE)


# --- Helpers -------------------------------------------------------------------


def add_account(core, db, label="Main", *, kind="whatsapp", paired=True, desired="running", port=None, color="green"):
    cur = db.execute(
        "INSERT INTO accounts (kind, label, color, desired, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (kind, label, color, desired, NOW, NOW),
    )
    account_id = cur.lastrowid
    if kind == "whatsapp":
        db.execute(
            "UPDATE accounts SET port = ?, session_dir = ? WHERE id = ?",
            (port if port is not None else 4000 + account_id, f"sessions/{account_id}", account_id),
        )
        if paired:
            session = core.db.data_dir() / f"sessions/{account_id}"
            session.mkdir(parents=True, exist_ok=True)
            (session / "creds.json").write_text("{}", encoding="utf-8")
    return account_id


def account_port(db, account_id):
    return db.execute("SELECT port FROM accounts WHERE id = ?", (account_id,)).fetchone()[0]


def add_conv(
    db,
    account_id,
    jid,
    state,
    *,
    name=None,
    last_at=None,
    priority=0,
    tags=None,
    agent_active=1,
    unread=0,
    muted_until=None,
    previous_state=None,
    last_inbound_at=None,
):
    last_at = last_at if last_at is not None else NOW - 60
    cur = db.execute(
        "INSERT INTO conversations (account_id, chat_jid, contact_name, phone, state, previous_state, priority,"
        " muted_until, last_message_at, last_inbound_at, unread_count, agent_active, tags, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            account_id,
            jid,
            name,
            jid.split("@")[0],
            state,
            previous_state,
            priority,
            muted_until,
            last_at,
            last_inbound_at,
            unread,
            agent_active,
            json.dumps(tags or []),
            last_at,
            last_at,
        ),
    )
    return cur.lastrowid


def add_msg(db, conv_id, direction, body, ts, *, author=None, status=None, source="live", meta=None, wa_id=None):
    account_id = db.execute("SELECT account_id FROM conversations WHERE id = ?", (conv_id,)).fetchone()[0]
    cur = db.execute(
        "INSERT INTO messages (conversation_id, account_id, wa_id, direction, author, body, ts, status, source, meta)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            conv_id,
            account_id,
            wa_id,
            direction,
            author or ("contact" if direction == "in" else "user"),
            body,
            ts,
            status or ("received" if direction == "in" else "sent"),
            source,
            json.dumps(meta) if meta else None,
        ),
    )
    return cur.lastrowid


def wa_event(**overrides):
    event = {
        "messageId": "M1",
        "chatId": CONTACT_JID,
        "senderId": CONTACT_JID,
        "senderName": "Mario",
        "isGroup": False,
        "body": "hello",
        "hasMedia": False,
        "mediaType": "",
        "mediaUrls": [],
        "botIds": [OWN_JID],
        "fromOwner": False,
        "timestamp": NOW,
    }
    event.update(overrides)
    return event


def owner_event(**overrides):
    return wa_event(senderId=OWN_JID, fromOwner=True, senderName="Me", **overrides)


def set_rules(core, db, **rules):
    s = core.settings.get_settings(db)
    core.settings.save_settings(db, s.model_copy(update={"rules": s.rules.model_copy(update=rules)}), NOW)


def conv_row(db, conv_id):
    return db.execute("SELECT * FROM conversations WHERE id = ?", (conv_id,)).fetchone()


def conv_by_jid(db, account_id, jid):
    return db.execute("SELECT * FROM conversations WHERE account_id = ? AND chat_jid = ?", (account_id, jid)).fetchone()


def log_rows(db, conv_id):
    return db.execute(
        "SELECT * FROM conversation_state_log WHERE conversation_id = ? ORDER BY id", (conv_id,)
    ).fetchall()


def event_types(db):
    return [r[0] for r in db.execute("SELECT type FROM events ORDER BY id")]


def event_payloads(db, type_):
    return [json.loads(r[0]) if r[0] else None for r in db.execute("SELECT payload FROM events WHERE type = ? ORDER BY id", (type_,))]


def columns(client, **params):
    body = client.get(f"{PREFIX}/board", params=params).json()
    return {c["name"]: c["cards"] for c in body["columns"]}


def post(client, conv_id, action, **body):
    return client.post(f"{PREFIX}/conversations/{conv_id}/{action}", json=body)


def messages(client, conv_id, **params):
    return client.get(f"{PREFIX}/conversations/{conv_id}/messages", params=params).json()


# --- GET /health, /service -------------------------------------------------------


def test_health_reports_db_count_and_schema_version(client, db, account):
    add_conv(db, account, "a@s.whatsapp.net", "new")
    add_conv(db, account, "b@s.whatsapp.net", "closed")
    body = client.get(f"{PREFIX}/health").json()
    assert body["ok"] is True
    assert body["conversations"] == 2
    assert body["schema_version"] == 2
    assert body["db"].endswith("wa_board.db")


def test_unopenable_db_returns_500_with_actionable_detail(client, plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("WA_ARCHIVE_DB", str(tmp_path))  # a directory, not a database
    r = client.get(f"{PREFIX}/board")
    assert r.status_code == 500
    assert "board read failed" in r.json()["detail"]
    assert client.get(f"{PREFIX}/health").json()["ok"] is False


def test_service_reports_running_only_for_a_fresh_heartbeat(client, core, db):
    body = client.get(f"{PREFIX}/service").json()
    assert body["running"] is False
    assert body["heartbeat_at"] is None
    assert "wa_channel.py" in body["install_command"]

    core.accounts.service_heartbeat(db, pid=4242, version="2.0", started_at=NOW - 100, now=int(time.time()))
    body = client.get(f"{PREFIX}/service").json()
    assert (body["running"], body["pid"], body["version"]) == (True, 4242, "2.0")

    core.accounts.service_heartbeat(db, pid=4242, version="2.0", started_at=NOW - 100, now=int(time.time()) - 60)
    assert client.get(f"{PREFIX}/service").json()["running"] is False


# --- Migration v1 -> v2 -----------------------------------------------------------

V1_SCHEMA = """
CREATE TABLE conversations (
  chat_jid TEXT PRIMARY KEY, contact_name TEXT, phone TEXT,
  state TEXT NOT NULL DEFAULT 'new', priority INTEGER NOT NULL DEFAULT 0,
  muted_until INTEGER, last_message_at INTEGER, unread_count INTEGER NOT NULL DEFAULT 0,
  agent_active INTEGER NOT NULL DEFAULT 1, tags TEXT NOT NULL DEFAULT '[]', updated_at INTEGER);
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_jid TEXT NOT NULL, wa_id TEXT,
  direction TEXT NOT NULL CHECK (direction IN ('in','out')), body TEXT, ts INTEGER NOT NULL, meta TEXT);
CREATE TABLE conversation_state_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, chat_jid TEXT NOT NULL, from_state TEXT,
  to_state TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT, at INTEGER NOT NULL);
CREATE INDEX idx_messages_chat_ts ON messages(chat_jid, ts);
CREATE UNIQUE INDEX idx_messages_wa_id ON messages(wa_id) WHERE wa_id IS NOT NULL;
CREATE INDEX idx_conversations_state ON conversations(state);
CREATE INDEX idx_state_log_chat ON conversation_state_log(chat_jid, id);
"""


def build_v1(path, *, real=True, demo=True):
    conn = sqlite3.connect(path)
    conn.executescript(V1_SCHEMA)
    if real:
        conn.execute(
            "INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (CONTACT_JID, "Mario", "393330001111", "waiting", 1, None, NOW - 50, 0, 1, '["case"]', NOW - 50),
        )
        conn.executemany(
            "INSERT INTO messages (chat_jid, wa_id, direction, body, ts, meta) VALUES (?,?,?,?,?,?)",
            [
                (CONTACT_JID, "A1", "in", "hi", NOW - 300, None),
                (CONTACT_JID, "A2", "out", "hello", NOW - 200, None),
                (CONTACT_JID, "A3", "in", "", NOW - 100, json.dumps({"mediaType": "image", "mediaUrls": ["/tmp/x/pic.jpg"]})),
            ],
        )
        conn.execute(
            "INSERT INTO conversation_state_log (chat_jid, from_state, to_state, actor, reason, at)"
            " VALUES (?,NULL,'new','auto','first message',?)",
            (CONTACT_JID, NOW - 300),
        )
    if demo:
        conn.execute(
            "INSERT INTO conversations VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            ("5551@demo.invalid", "Demo Dan", "5551", "new", 0, None, NOW - 20, 1, 1, "[]", NOW - 20),
        )
        conn.execute(
            "INSERT INTO messages (chat_jid, wa_id, direction, body, ts) VALUES (?,NULL,'in','demo hi',?)",
            ("5551@demo.invalid", NOW - 20),
        )
    conn.commit()
    conn.close()


def test_v1_db_migrates_keeping_conversations_and_creating_accounts(client, core, tmp_path):
    build_v1(tmp_path / "wa_board.db")

    accounts = {a["label"]: a for a in client.get(f"{PREFIX}/accounts").json()["accounts"]}
    assert set(accounts) == {"Main", "Demo"}
    main, demo = accounts["Main"], accounts["Demo"]
    assert (main["id"], main["kind"], main["color"], main["port"]) == (1, "whatsapp", "green", 3017)
    assert (demo["kind"], demo["color"], demo["port"], demo["status"]) == ("demo", "gray", None, None)

    listing = client.get(f"{PREFIX}/conversations").json()
    assert listing["total"] == 2
    by_jid = {c["chat_jid"]: c for c in listing["conversations"]}
    mario = by_jid[CONTACT_JID]
    assert (mario["account_id"], mario["state"], mario["priority"], mario["tags"]) == (main["id"], "waiting", 1, ["case"])
    assert by_jid["5551@demo.invalid"]["account_id"] == demo["id"]

    thread = messages(client, mario["id"])["messages"]
    assert [(m["direction"], m["author"], m["status"], m["source"]) for m in thread] == [
        ("in", "contact", "received", "live"),
        ("out", "user", "sent", "live"),
        ("in", "contact", "received", "live"),
    ]
    assert thread[2]["media"][0]["name"] == "pic.jpg"

    detail = client.get(f"{PREFIX}/conversations/{mario['id']}").json()
    assert detail["state_history"][0]["to_state"] == "new"
    conn = core.db.connect()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 2
        assert conn.execute("SELECT last_inbound_at FROM conversations WHERE chat_jid = ?", (CONTACT_JID,)).fetchone()[0] == NOW - 100
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert not any(t.startswith("v1_") for t in tables)
    finally:
        conn.close()


def test_v1_db_with_only_demo_data_creates_only_the_demo_account(client, tmp_path):
    build_v1(tmp_path / "wa_board.db", real=False)
    accounts = client.get(f"{PREFIX}/accounts").json()["accounts"]
    assert [(a["label"], a["kind"]) for a in accounts] == [("Demo", "demo")]


def test_v1_db_with_session_creds_creates_main_even_without_conversations(client, core, tmp_path):
    build_v1(tmp_path / "wa_board.db", real=False, demo=False)
    session = core.db.data_dir() / "wa-session"
    session.mkdir(parents=True)
    (session / "creds.json").write_text("{}", encoding="utf-8")
    (main,) = client.get(f"{PREFIX}/accounts").json()["accounts"]
    assert (main["label"], main["paired"], main["port"]) == ("Main", True, 3017)


def test_fresh_db_has_no_accounts(client):
    assert client.get(f"{PREFIX}/accounts").json() == {"accounts": []}


# --- GET /board ----------------------------------------------------------------


def test_empty_db_returns_five_empty_columns(client):
    body = client.get(f"{PREFIX}/board").json()
    assert [c["name"] for c in body["columns"]] == ["new", "in_progress", "waiting", "muted", "closed"]
    assert all(c["cards"] == [] for c in body["columns"])


def test_board_groups_cards_and_builds_payload(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new", name="Anna", last_at=NOW - 120, unread=2, tags=["case"])
    add_msg(db, a, "out", "older", NOW - 500)
    add_msg(db, a, "in", "where is my order?", NOW - 120)
    add_conv(db, account, "b@s.whatsapp.net", "waiting", name="Bruno")

    cols = columns(client)
    assert [c["contact_name"] for c in cols["new"]] == ["Anna"]
    assert [c["contact_name"] for c in cols["waiting"]] == ["Bruno"]
    card = cols["new"][0]
    assert card["id"] == a
    assert (card["account_id"], card["account_label"], card["account_color"]) == (account, "Main", "green")
    assert card["chat_jid"] == "a@s.whatsapp.net"
    assert card["last_message_preview"] == "where is my order?"
    assert (card["last_message_direction"], card["last_message_author"]) == ("in", "contact")
    assert 119 <= card["age_seconds"] < 1000
    assert (card["unread_count"], card["agent_active"], card["tags"]) == (2, True, ["case"])
    assert (card["urgency"], card["has_draft"]) == ("normal", False)


def test_board_preview_truncated_to_200_chars(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new")
    add_msg(db, a, "in", "x" * 500, NOW - 60)
    assert len(columns(client)["new"][0]["last_message_preview"]) == 200


def test_unknown_state_lands_in_fallback_column_and_can_move(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "weird")
    assert columns(client)["in_progress"][0]["state"] == "weird"
    assert post(client, a, "state", state="waiting").status_code == 200
    assert columns(client)["waiting"][0]["chat_jid"] == "a@s.whatsapp.net"


def test_board_excludes_closed_unless_requested_and_limits_them(client, db, account, core):
    for i in range(3):
        add_conv(db, account, f"{i}@s.whatsapp.net", "closed", name=f"Done {i}", last_at=NOW - 1000 - i)
    assert columns(client)["closed"] == []
    assert [c["contact_name"] for c in columns(client, include_closed="true")["closed"]] == ["Done 0", "Done 1", "Done 2"]

    settings = client.get(f"{PREFIX}/settings").json()
    settings["board"]["closed_limit"] = 2
    assert client.put(f"{PREFIX}/settings", json=settings).status_code == 200
    assert [c["contact_name"] for c in columns(client, include_closed="true")["closed"]] == ["Done 0", "Done 1"]


def test_board_search_filters_by_name_or_phone_and_account(client, core, db, account):
    other = add_account(core, db, "Second", color="blue")
    add_conv(db, account, "111@s.whatsapp.net", "new", name="Alice Martin")
    add_conv(db, account, "222@s.whatsapp.net", "new", name="Bob Stone")
    add_conv(db, other, "111@s.whatsapp.net", "new", name="Alice Martin")
    assert len(columns(client, q="alice")["new"]) == 2
    assert [c["contact_name"] for c in columns(client, q="222")["new"]] == ["Bob Stone"]
    assert len(columns(client, q="  ")["new"]) == 3
    only_second = columns(client, account_id=other)["new"]
    assert [(c["account_label"], c["account_color"]) for c in only_second] == [("Second", "blue")]
    assert len(columns(client, account_id=account, q="alice")["new"]) == 1


def test_escalated_card_sorted_first_with_high_urgency(client, db, account):
    add_conv(db, account, "old@s.whatsapp.net", "new", last_at=NOW - 600)
    add_conv(db, account, "hot@s.whatsapp.net", "new", last_at=NOW - 60, priority=2)
    cards = columns(client)["new"]
    assert [c["chat_jid"] for c in cards] == ["hot@s.whatsapp.net", "old@s.whatsapp.net"]
    assert cards[0]["urgency"] == "high"


def test_stale_new_card_has_medium_urgency_per_setting(client, db, account):
    add_conv(db, account, "a@s.whatsapp.net", "new", last_at=NOW - 2 * 86400)
    add_conv(db, account, "b@s.whatsapp.net", "in_progress", last_at=NOW - 2 * 86400)
    assert columns(client)["new"][0]["urgency"] == "medium"
    assert columns(client)["in_progress"][0]["urgency"] == "normal"

    settings = client.get(f"{PREFIX}/settings").json()
    settings["board"]["urgency_hours"] = 72
    client.put(f"{PREFIX}/settings", json=settings)
    assert columns(client)["new"][0]["urgency"] == "normal"


def test_media_only_message_previews_its_type(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new")
    add_msg(db, a, "in", "", NOW - 60, meta={"mediaType": "image", "media": []})
    assert columns(client)["new"][0]["last_message_preview"] == "[image]"


# --- GET /conversations, detail, messages, search ------------------------------------


def test_conversation_list_is_newest_first_includes_closed_and_filters(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "closed", name="Old", last_at=NOW - 900)
    b = add_conv(db, account, "b@s.whatsapp.net", "new", name="Mid", last_at=NOW - 500, unread=2)
    c = add_conv(db, account, "c@s.whatsapp.net", "waiting", name="Fresh", last_at=NOW - 10)
    body = client.get(f"{PREFIX}/conversations").json()
    assert [x["id"] for x in body["conversations"]] == [c, b, a]
    assert body["total"] == 3

    unread = client.get(f"{PREFIX}/conversations", params={"unread_only": "true"}).json()
    assert [x["id"] for x in unread["conversations"]] == [b]
    assert [x["id"] for x in client.get(f"{PREFIX}/conversations", params={"state": "closed"}).json()["conversations"]] == [a]
    assert [x["id"] for x in client.get(f"{PREFIX}/conversations", params={"q": "fresh"}).json()["conversations"]] == [c]

    page = client.get(f"{PREFIX}/conversations", params={"limit": 1, "offset": 1}).json()
    assert ([x["id"] for x in page["conversations"]], page["total"]) == ([b], 3)


def test_conversation_detail_has_account_allowed_states_history_and_runs(client, core, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new", name="Anna", tags=["info"])
    post(client, a, "state", state="in_progress")
    rule = db.execute(
        "INSERT INTO automation_rules (name, action, created_at) VALUES ('r', '{}', ?)", (NOW,)
    ).lastrowid
    db.execute(
        "INSERT INTO automation_runs (rule_id, event_id, conversation_id, status, created_at) VALUES (?,1,?,'done',?)",
        (rule, a, NOW),
    )

    body = client.get(f"{PREFIX}/conversations/{a}").json()
    conversation = body["conversation"]
    assert (conversation["state"], conversation["tags"], conversation["previous_state"]) == ("in_progress", ["info"], "new")
    assert conversation["created_at"] is not None
    assert body["account"]["id"] == account
    assert body["allowed_states"] == ["closed", "muted", "waiting"]
    assert body["state_history"][0]["to_state"] == "in_progress"
    assert [r["status"] for r in body["runs"]] == ["done"]
    assert client.get(f"{PREFIX}/conversations/9999").status_code == 404


def test_messages_paginate_with_before_id_and_has_more(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new")
    ids = [add_msg(db, a, "in", f"m{i}", NOW - 100 + i) for i in range(1, 6)]
    first = messages(client, a, limit=2)
    assert [m["body"] for m in first["messages"]] == ["m4", "m5"]
    assert first["has_more"] is True
    second = messages(client, a, limit=2, before_id=ids[3])
    assert [m["body"] for m in second["messages"]] == ["m2", "m3"]
    assert second["has_more"] is True
    third = messages(client, a, limit=2, before_id=ids[1])
    assert [m["body"] for m in third["messages"]] == ["m1"]
    assert third["has_more"] is False
    assert set(first["messages"][0]) >= {
        "id", "conversation_id", "direction", "author", "body", "ts", "status", "source", "wa_id", "error", "rule_id", "media",
    }
    assert client.get(f"{PREFIX}/conversations/9999/messages").status_code == 404


def test_search_matches_bodies_with_card_and_account_filter(client, core, db, account):
    other = add_account(core, db, "Second")
    a = add_conv(db, account, "a@s.whatsapp.net", "new", name="Anna")
    b = add_conv(db, other, "b@s.whatsapp.net", "new", name="Bruno")
    add_msg(db, a, "in", "where is my invoice?", NOW - 200)
    add_msg(db, b, "in", "invoice 100% paid", NOW - 100)
    add_msg(db, a, "out", "secret invoice draft", NOW - 50, status="draft")

    results = client.get(f"{PREFIX}/search", params={"q": "invoice"}).json()["results"]
    assert [r["conversation"]["id"] for r in results] == [b, a]
    assert results[0]["message"]["body"] == "invoice 100% paid"
    assert results[0]["conversation"]["account_label"] == "Second"
    only = client.get(f"{PREFIX}/search", params={"q": "invoice", "account_id": account}).json()["results"]
    assert [r["conversation"]["id"] for r in only] == [a]
    assert client.get(f"{PREFIX}/search", params={"q": "%"}).json()["results"][0]["conversation"]["id"] == b
    assert client.get(f"{PREFIX}/search", params={"q": ""}).json() == {"results": []}


# --- State routes --------------------------------------------------------------


def test_valid_transition_persists_logs_server_actor_and_emits(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new")
    r = post(client, a, "state", state="in_progress", reason="on it", actor="agent")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert (body["id"], body["state"], body["agent_active"], body["priority"]) == (a, "in_progress", True, 0)
    assert set(body) >= {"muted_until", "unread_count", "tags"}
    (entry,) = log_rows(db, a)
    assert (entry["from_state"], entry["to_state"], entry["actor"], entry["reason"]) == ("new", "in_progress", "user", "on it")
    assert conv_row(db, a)["previous_state"] == "new"
    assert event_payloads(db, "conversation.state_changed") == [
        {"from": "new", "to": "in_progress", "actor": "user", "reason": "on it"}
    ]


def test_invalid_transition_409_names_current_and_allowed(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "waiting")
    r = post(client, a, "state", state="new")
    assert r.status_code == 409
    assert "'waiting'" in r.json()["detail"]
    assert "in_progress" in r.json()["detail"]
    assert conv_row(db, a)["state"] == "waiting"
    assert log_rows(db, a) == []
    assert post(client, add_conv(db, account, "z@s.whatsapp.net", "closed"), "state", state="new").status_code == 409


def test_unknown_conversation_404_and_unknown_state_400(client, db, account):
    assert post(client, 9999, "state", state="closed").status_code == 404
    a = add_conv(db, account, "a@s.whatsapp.net", "new")
    r = post(client, a, "state", state="bogus")
    assert r.status_code == 400
    assert "bogus" in r.json()["detail"]


def test_mute_requires_future_timestamp(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new")
    assert post(client, a, "mute", muted_until=int(time.time()) - 5).status_code == 400
    assert post(client, a, "state", state="muted").status_code == 400
    until = int(time.time()) + 3600
    r = post(client, a, "mute", muted_until=until)
    assert r.status_code == 200
    row = conv_row(db, a)
    assert (row["state"], row["muted_until"]) == ("muted", until)
    assert r.json()["muted_until"] == until
    # leaving muted clears the snooze
    assert post(client, a, "state", state="in_progress").json()["muted_until"] is None


def test_expired_mute_reopens_as_new_on_board_read(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "muted", muted_until=int(time.time()) - 10)
    assert [c["chat_jid"] for c in columns(client)["new"]] == ["a@s.whatsapp.net"]
    assert conv_row(db, a)["muted_until"] is None
    (entry,) = log_rows(db, a)
    assert (entry["from_state"], entry["to_state"], entry["actor"], entry["reason"]) == ("muted", "new", "auto", "mute expired")


def test_takeover_moves_to_in_progress_and_disables_agent(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new")
    b = add_conv(db, account, "b@s.whatsapp.net", "closed")
    c = add_conv(db, account, "c@s.whatsapp.net", "in_progress")
    for conv in (a, b, c):
        r = post(client, conv, "takeover")
        assert r.status_code == 200
        assert (r.json()["state"], r.json()["agent_active"]) == ("in_progress", False)
    (entry,) = log_rows(db, c)  # already in progress: no state change, but audited
    assert (entry["from_state"], entry["to_state"], entry["reason"]) == ("in_progress", "in_progress", "takeover")
    post(client, c, "takeover")  # repeating is a no-op
    assert len(log_rows(db, c)) == 1
    assert "conversation.updated" in event_types(db)


def test_handback_reenables_agent(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "in_progress", agent_active=0)
    assert post(client, a, "handback").json()["agent_active"] is True
    post(client, a, "handback")
    (entry,) = log_rows(db, a)
    assert entry["reason"] == "handback"


def test_escalate_and_deescalate_set_priority(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new")
    assert post(client, a, "escalate", escalated=True).json()["priority"] == 2
    assert post(client, a, "escalate", escalated=False).json()["priority"] == 0
    assert [e["reason"] for e in log_rows(db, a)] == ["escalated", "de-escalated"]
    assert {"fields": ["priority"]} in event_payloads(db, "conversation.updated")


def test_mark_read_clears_unread_without_logging(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new", unread=3)
    assert post(client, a, "read").json()["unread_count"] == 0
    assert log_rows(db, a) == []
    assert {"fields": ["unread_count"]} in event_payloads(db, "conversation.updated")


def test_tags_are_replaced_normalized_and_validated(client, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new", tags=["old"])
    r = client.put(f"{PREFIX}/conversations/{a}/tags", json={"tags": [" vip ", "case", "vip", ""]})
    assert r.status_code == 200
    assert r.json()["tags"] == ["vip", "case"]
    assert json.loads(conv_row(db, a)["tags"]) == ["vip", "case"]
    assert client.put(f"{PREFIX}/conversations/{a}/tags", json={"tags": ["x" * 41]}).status_code == 400
    assert client.put(f"{PREFIX}/conversations/9999/tags", json={"tags": []}).status_code == 404
    assert {"fields": ["tags"]} in event_payloads(db, "conversation.updated")


# --- Rules engine: inbound -----------------------------------------------------------


def test_first_inbound_creates_new_conversation_with_audit_and_events(core, db, account):
    assert core.ingest.ingest_event(db, account, wa_event(), NOW) is not None
    row = conv_by_jid(db, account, CONTACT_JID)
    assert (row["state"], row["unread_count"], row["contact_name"], row["phone"]) == ("new", 1, "Mario", "393330001111")
    assert row["last_inbound_at"] == NOW
    (entry,) = log_rows(db, row["id"])
    assert (entry["from_state"], entry["to_state"], entry["actor"]) == (None, "new", "auto")
    types = event_types(db)
    for expected in ("conversation.created", "conversation.state_changed", "message.in"):
        assert expected in types
    assert event_payloads(db, "conversation.state_changed")[0]["from"] is None


def test_inbound_dedups_per_account_and_ignores_groups(core, db, account):
    other = add_account(core, db, "Second")
    first = core.ingest.ingest_event(db, account, wa_event(), NOW)
    assert first is not None
    assert core.ingest.ingest_event(db, account, wa_event(), NOW) is None
    assert core.ingest.ingest_event(db, account, wa_event(messageId="G1", isGroup=True), NOW) is None
    assert core.ingest.ingest_event(db, account, wa_event(messageId="G2", chatId="123-456@g.us", senderId="x@lid"), NOW) is None
    # the same wa_id on another number is a different message, and a different conversation
    assert core.ingest.ingest_event(db, other, wa_event(), NOW) is not None
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2
    ids = {conv_by_jid(db, a, CONTACT_JID)["id"] for a in (account, other)}
    assert len(ids) == 2


def test_inbound_prefers_phone_jid_over_lid_and_skips_own_number(core, db, account):
    core.ingest.ingest_event(db, account, wa_event(messageId="L1", chatId="5551234@lid", senderId=CONTACT_JID), NOW)
    assert conv_by_jid(db, account, CONTACT_JID) is not None
    assert conv_by_jid(db, account, "5551234@lid") is None
    # no phone JID available: the LID stays the key
    core.ingest.ingest_event(db, account, wa_event(messageId="L2", chatId="777@lid", senderId="777@lid"), NOW)
    assert conv_by_jid(db, account, "777@lid") is not None
    # typed on the dedicated phone: the sender is our own number, the chat is the contact
    core.ingest.ingest_event(
        db, account, owner_event(messageId="O1", chatId="393330002222@s.whatsapp.net"), NOW
    )
    assert conv_by_jid(db, account, "393330002222@s.whatsapp.net") is not None
    assert conv_by_jid(db, account, OWN_JID) is None


def test_inbound_parses_long_timestamp(core, db, account):
    core.ingest.ingest_event(db, account, wa_event(messageId="T1", timestamp={"low": NOW - 1000, "high": 0}), NOW)
    assert db.execute("SELECT ts FROM messages WHERE wa_id='T1'").fetchone()[0] == NOW - 1000
    core.ingest.ingest_event(db, account, wa_event(messageId="T2", timestamp={"low": 5, "high": 9}), NOW)
    assert db.execute("SELECT ts FROM messages WHERE wa_id='T2'").fetchone()[0] == NOW


def test_inbound_on_closed_reopens_as_new_by_default(core, db, account):
    a = add_conv(db, account, CONTACT_JID, "closed", unread=0)
    core.ingest.ingest_event(db, account, wa_event(), NOW)
    row = conv_row(db, a)
    assert (row["state"], row["unread_count"], row["previous_state"]) == ("new", 1, "closed")
    entry = log_rows(db, a)[-1]
    assert (entry["from_state"], entry["to_state"], entry["actor"]) == ("closed", "new", "auto")
    assert "inbound_on_closed" in entry["reason"]


def test_inbound_on_closed_reopen_previous_and_keep(core, db, account):
    set_rules(core, db, inbound_on_closed="reopen_previous")
    a = add_conv(db, account, "1@s.whatsapp.net", "closed", previous_state="in_progress")
    b = add_conv(db, account, "2@s.whatsapp.net", "closed")  # no previous state: falls back to new
    core.ingest.ingest_event(db, account, wa_event(messageId="P1", chatId="1@s.whatsapp.net", senderId="1@s.whatsapp.net"), NOW)
    core.ingest.ingest_event(db, account, wa_event(messageId="P2", chatId="2@s.whatsapp.net", senderId="2@s.whatsapp.net"), NOW)
    assert (conv_row(db, a)["state"], conv_row(db, b)["state"]) == ("in_progress", "new")

    set_rules(core, db, inbound_on_closed="keep")
    c = add_conv(db, account, "3@s.whatsapp.net", "closed")
    core.ingest.ingest_event(db, account, wa_event(messageId="P3", chatId="3@s.whatsapp.net", senderId="3@s.whatsapp.net"), NOW)
    row = conv_row(db, c)
    assert (row["state"], row["unread_count"]) == ("closed", 1)


def test_inbound_on_waiting_moves_to_in_progress_unless_keep(core, db, account):
    a = add_conv(db, account, CONTACT_JID, "waiting")
    core.ingest.ingest_event(db, account, wa_event(), NOW)
    assert conv_row(db, a)["state"] == "in_progress"

    set_rules(core, db, inbound_on_waiting="keep")
    b = add_conv(db, account, "2@s.whatsapp.net", "waiting")
    core.ingest.ingest_event(db, account, wa_event(messageId="W2", chatId="2@s.whatsapp.net", senderId="2@s.whatsapp.net"), NOW)
    assert conv_row(db, b)["state"] == "waiting"


def test_inbound_on_muted_reopens_but_not_on_reaction_or_keep(core, db, account):
    reacting = "393330003333@s.whatsapp.net"
    a = add_conv(db, account, CONTACT_JID, "muted", muted_until=NOW + 3600)
    b = add_conv(db, account, reacting, "muted", muted_until=NOW + 3600)

    core.ingest.ingest_event(
        db, account, wa_event(messageId="R1", chatId=reacting, senderId=reacting, body="x", mediaType="reaction"), NOW
    )
    row = conv_row(db, b)
    assert (row["state"], row["unread_count"]) == ("muted", 1)

    core.ingest.ingest_event(db, account, wa_event(messageId="R2"), NOW)
    row = conv_row(db, a)
    assert (row["state"], row["muted_until"]) == ("new", None)
    entry = log_rows(db, a)[-1]
    assert (entry["from_state"], entry["to_state"], entry["actor"]) == ("muted", "new", "auto")

    set_rules(core, db, inbound_on_muted="keep")
    c = add_conv(db, account, "4@s.whatsapp.net", "muted", muted_until=NOW + 3600)
    core.ingest.ingest_event(db, account, wa_event(messageId="R3", chatId="4@s.whatsapp.net", senderId="4@s.whatsapp.net"), NOW)
    assert conv_row(db, c)["state"] == "muted"


def test_inbound_on_active_states_only_counts_unread(core, db, account):
    a = add_conv(db, account, CONTACT_JID, "in_progress", unread=1, name=None)
    core.ingest.ingest_event(db, account, wa_event(), NOW)
    row = conv_row(db, a)
    assert (row["state"], row["unread_count"], row["contact_name"]) == ("in_progress", 2, "Mario")
    assert log_rows(db, a) == []


# --- Rules engine: owner messages and history ---------------------------------------


def test_phone_reply_moves_active_conversation_to_waiting_and_clears_unread(core, db, account):
    a = add_conv(db, account, CONTACT_JID, "new", unread=2, last_inbound_at=NOW - 3600)
    mid = core.ingest.ingest_event(db, account, owner_event(messageId="O1", body="typed on phone"), NOW)
    row = conv_row(db, a)
    assert (row["state"], row["unread_count"]) == ("waiting", 0)
    msg = db.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
    assert (msg["direction"], msg["author"], msg["status"]) == ("out", "phone", "sent")
    assert "message.out" in event_types(db)

    set_rules(core, db, outbound_on_active="keep")
    b = add_conv(db, account, "2@s.whatsapp.net", "in_progress", unread=1, last_inbound_at=NOW - 3600)
    core.ingest.ingest_event(db, account, owner_event(messageId="O2", chatId="2@s.whatsapp.net"), NOW)
    row = conv_row(db, b)
    assert (row["state"], row["unread_count"]) == ("in_progress", 0)


def test_phone_message_within_auto_reply_window_is_auto_reply(core, db, account):
    core.ingest.ingest_event(db, account, wa_event(timestamp=NOW - 30), NOW)
    row = conv_by_jid(db, account, CONTACT_JID)
    state_before = (row["state"], row["unread_count"])

    auto = core.ingest.ingest_event(db, account, owner_event(messageId="AUTO", timestamp=NOW - 25, body="We are closed"), NOW)
    row = conv_by_jid(db, account, CONTACT_JID)
    assert db.execute("SELECT author FROM messages WHERE id = ?", (auto,)).fetchone()[0] == "auto_reply"
    assert (row["state"], row["unread_count"]) == state_before == ("new", 1)

    human = core.ingest.ingest_event(db, account, owner_event(messageId="HUMAN", timestamp=NOW - 5, body="Hi!"), NOW)
    row = conv_by_jid(db, account, CONTACT_JID)
    assert db.execute("SELECT author FROM messages WHERE id = ?", (human,)).fetchone()[0] == "phone"
    assert (row["state"], row["unread_count"]) == ("waiting", 0)


def test_auto_reply_window_zero_disables_detection(core, db, account):
    set_rules(core, db, auto_reply_window_seconds=0)
    core.ingest.ingest_event(db, account, wa_event(timestamp=NOW - 3), NOW)
    mid = core.ingest.ingest_event(db, account, owner_event(messageId="O1", timestamp=NOW - 2), NOW)
    assert db.execute("SELECT author FROM messages WHERE id = ?", (mid,)).fetchone()[0] == "phone"


def test_owner_message_first_creates_waiting_conversation(core, db, account):
    fresh = "393330004444@s.whatsapp.net"
    core.ingest.ingest_event(db, account, owner_event(messageId="O2", chatId=fresh), NOW)
    row = conv_by_jid(db, account, fresh)
    assert (row["state"], row["unread_count"], row["contact_name"]) == ("waiting", 0, None)
    assert row["last_inbound_at"] is None
    assert "conversation.created" in event_types(db)


def test_history_creates_closed_conversation_and_triggers_nothing(core, db, account):
    older = NOW - 40 * 86400
    first = core.ingest.ingest_event(db, account, wa_event(messageId="H1", timestamp=older), NOW, source="history")
    second = core.ingest.ingest_event(
        db, account, owner_event(messageId="H2", timestamp=older + 60, body="old reply"), NOW, source="history"
    )
    assert first and second
    row = conv_by_jid(db, account, CONTACT_JID)
    assert (row["state"], row["unread_count"], row["last_message_at"]) == ("closed", 0, older + 60)
    rows = db.execute("SELECT direction, author, status, source FROM messages ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [
        ("in", "contact", "received", "history"),
        ("out", "phone", "sent", "history"),
    ]
    assert event_types(db) == []
    assert core.ingest.ingest_event(db, account, wa_event(messageId="H1", timestamp=older), NOW, source="history") is None


def test_history_leaves_an_existing_conversation_untouched(core, db, account):
    a = add_conv(db, account, CONTACT_JID, "in_progress", unread=2, last_at=NOW - 10, last_inbound_at=NOW - 10)
    core.ingest.ingest_event(db, account, wa_event(messageId="H1", timestamp=NOW - 86400), NOW, source="history")
    row = conv_row(db, a)
    assert (row["state"], row["unread_count"], row["last_message_at"], row["last_inbound_at"]) == (
        "in_progress",
        2,
        NOW - 10,
        NOW - 10,
    )
    assert log_rows(db, a) == []
    assert event_types(db) == []


def test_inbound_media_is_recorded_and_served_from_allowed_roots_only(client, core, db, account, tmp_path):
    cache = core.db.data_dir() / "wa-media" / str(account) / "image"
    cache.mkdir(parents=True)
    inside = cache / "pic.jpg"
    inside.write_bytes(b"\xff\xd8jpeg-bytes")
    outside = tmp_path / "outside" / "secret.jpg"
    outside.parent.mkdir()
    outside.write_bytes(b"secret")
    sneaky = cache.as_posix() + "/" + "../" * 6 + "outside/secret.jpg"

    mid_in = core.ingest.ingest_event(
        db, account, wa_event(messageId="MI", hasMedia=True, mediaType="image", mediaUrls=[str(inside)], body=""), NOW
    )
    mid_out = core.ingest.ingest_event(
        db, account, wa_event(messageId="MO", hasMedia=True, mediaType="image", mediaUrls=[str(outside)], body=""), NOW
    )
    mid_dotdot = core.ingest.ingest_event(
        db, account, wa_event(messageId="MD", hasMedia=True, mediaType="image", mediaUrls=[sneaky], body=""), NOW
    )

    conv = conv_by_jid(db, account, CONTACT_JID)["id"]
    thread = {m["id"]: m for m in messages(client, conv)["messages"]}
    item = thread[mid_in]["media"][0]
    assert (item["index"], item["type"], item["mime"], item["name"], item["size"], item["available"]) == (
        0, "image", "image/jpeg", "pic.jpg", len(b"\xff\xd8jpeg-bytes"), True,
    )
    assert thread[mid_out]["media"][0]["available"] is False

    ok = client.get(f"{PREFIX}/messages/{mid_in}/media/0")
    assert ok.status_code == 200
    data = ok.json()
    assert data["mime"] == "image/jpeg" and data["name"] == "pic.jpg" and data["size"] == len(b"\xff\xd8jpeg-bytes")
    assert data["data_url"] == "data:image/jpeg;base64," + base64.b64encode(b"\xff\xd8jpeg-bytes").decode()

    assert client.get(f"{PREFIX}/messages/{mid_out}/media/0").status_code == 404
    assert client.get(f"{PREFIX}/messages/{mid_dotdot}/media/0").status_code == 404
    assert client.get(f"{PREFIX}/messages/{mid_in}/media/3").status_code == 404
    assert client.get(f"{PREFIX}/messages/9999/media/0").status_code == 404
    inside.unlink()
    assert client.get(f"{PREFIX}/messages/{mid_in}/media/0").status_code == 404


def test_media_over_inline_limit_is_413(client, core, db, account, monkeypatch):
    cache = core.db.data_dir() / "wa-media" / str(account)
    cache.mkdir(parents=True)
    big = cache / "big.bin"
    big.write_bytes(b"x" * 100)
    mid = core.ingest.ingest_event(
        db, account, wa_event(hasMedia=True, mediaType="document", mediaUrls=[str(big)], body=""), NOW
    )
    monkeypatch.setattr(core.media, "MAX_DATA_URL_BYTES", 50)
    assert client.get(f"{PREFIX}/messages/{mid}/media/0").status_code == 413


# --- Timers --------------------------------------------------------------------------


def test_run_timers_expires_mutes_and_returns_change_count(core, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "muted", muted_until=NOW - 10)
    b = add_conv(db, account, "b@s.whatsapp.net", "muted", muted_until=NOW + 3600)
    assert core.conversations.run_timers(db, NOW) == 1
    assert (conv_row(db, a)["state"], conv_row(db, a)["muted_until"], conv_row(db, a)["previous_state"]) == ("new", None, "muted")
    assert conv_row(db, b)["state"] == "muted"
    assert core.conversations.run_timers(db, NOW) == 0
    assert event_payloads(db, "conversation.state_changed")[-1]["reason"] == "mute expired"


def test_run_timers_auto_closes_stale_waiting_conversations(core, db, account):
    old = add_conv(db, account, "old@s.whatsapp.net", "waiting", last_at=NOW - 8 * 86400)
    fresh = add_conv(db, account, "fresh@s.whatsapp.net", "waiting", last_at=NOW - 86400)
    active = add_conv(db, account, "act@s.whatsapp.net", "in_progress", last_at=NOW - 30 * 86400)
    assert core.conversations.run_timers(db, NOW) == 1
    assert (conv_row(db, old)["state"], conv_row(db, fresh)["state"], conv_row(db, active)["state"]) == (
        "closed", "waiting", "in_progress",
    )
    entry = log_rows(db, old)[-1]
    assert (entry["actor"], entry["reason"]) == ("auto", "auto-close after 7 days")


def test_run_timers_auto_close_can_be_disabled(core, db, account):
    set_rules(core, db, auto_close_waiting_days=None)
    old = add_conv(db, account, "old@s.whatsapp.net", "waiting", last_at=NOW - 90 * 86400)
    assert core.conversations.run_timers(db, NOW) == 0
    assert conv_row(db, old)["state"] == "waiting"


def test_run_timers_fails_abandoned_pending_sends(core, db, account):
    a = add_conv(db, account, "a@s.whatsapp.net", "new")
    stuck = add_msg(db, a, "out", "hi", NOW - 600, status="pending")
    recent = add_msg(db, a, "out", "hi", NOW - 5, status="pending")
    assert core.conversations.run_timers(db, NOW) == 1
    statuses = {r[0]: r[1] for r in db.execute("SELECT id, status FROM messages")}
    assert (statuses[stuck], statuses[recent]) == ("failed", "pending")


# --- POST /reply ---------------------------------------------------------------


def test_reply_sends_via_bridge_and_records_outbound(client, core, db, account, bridge):
    a = add_conv(db, account, CONTACT_JID, "new", unread=3)
    bridge.replies["/send"] = (200, {"success": True, "messageId": "ABC"})

    r = post(client, a, "reply", text="  test reply  ")

    assert r.status_code == 200
    body = r.json()
    assert bridge.calls == [(account_port(db, account), "POST", "/send", {"chatId": CONTACT_JID, "message": "test reply"})]
    message = body["message"]
    assert (message["direction"], message["author"], message["status"], message["wa_id"], message["body"]) == (
        "out", "user", "sent", "ABC", "test reply",
    )
    assert (body["summary"]["state"], body["summary"]["unread_count"]) == ("waiting", 0)
    assert "message.out" in event_types(db)
    # the bridge echoing our own send back is a duplicate, not a second message
    assert core.ingest.ingest_event(db, account, owner_event(messageId="ABC", body="test reply"), NOW) is None
    assert [m["id"] for m in messages(client, a)["messages"]] == [message["id"]]


def test_reply_keeps_state_when_outbound_rule_is_keep(client, core, db, account, bridge):
    set_rules(core, db, outbound_on_active="keep")
    a = add_conv(db, account, CONTACT_JID, "in_progress")
    bridge.replies["/send"] = (200, {"success": True, "messageId": "K1"})
    assert post(client, a, "reply", text="hi").json()["summary"]["state"] == "in_progress"


def test_reply_rejects_blank_demo_unpaired_stopped_and_unknown(client, core, db, account, bridge):
    demo = add_account(core, db, "Demo", kind="demo", color="gray")
    unpaired = add_account(core, db, "Fresh", paired=False)
    stopped = add_account(core, db, "Off", desired="stopped")
    ok = add_conv(db, account, CONTACT_JID, "new")
    in_demo = add_conv(db, demo, "5551@demo.invalid", "new")
    in_unpaired = add_conv(db, unpaired, CONTACT_JID, "new")
    in_stopped = add_conv(db, stopped, CONTACT_JID, "new")
    assert post(client, ok, "reply", text="   ").status_code == 400
    assert post(client, in_demo, "reply", text="hi").status_code == 400
    assert post(client, in_unpaired, "reply", text="hi").status_code == 503
    assert post(client, in_stopped, "reply", text="hi").status_code == 503
    assert post(client, 9999, "reply", text="hi").status_code == 404
    assert bridge.calls == []
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_reply_maps_bridge_failures_and_persists_failed_messages(client, db, account, bridge):
    a = add_conv(db, account, CONTACT_JID, "new", unread=3)
    assert post(client, a, "reply", text="hi").status_code == 503  # bridge unreachable

    bridge.replies["/send"] = (503, {"error": "Not connected to WhatsApp"})
    r = post(client, a, "reply", text="hi")
    assert r.status_code == 503
    assert "Not connected to WhatsApp" in r.json()["detail"]

    bridge.replies["/send"] = (500, {"error": "boom"})
    r = post(client, a, "reply", text="hi")
    assert r.status_code == 502
    assert "boom" in r.json()["detail"]

    thread = messages(client, a)["messages"]
    assert [m["status"] for m in thread] == ["failed", "failed", "failed"]
    assert "boom" in thread[-1]["error"]
    row = conv_row(db, a)
    assert (row["state"], row["unread_count"]) == ("new", 3)  # a failed send changes nothing else
    assert event_payloads(db, "message.status").count({"status": "failed"}) == 3


# --- POST /reply-media -----------------------------------------------------------


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


def test_reply_media_saves_upload_and_posts_to_bridge(client, core, db, account, bridge):
    a = add_conv(db, account, CONTACT_JID, "new")
    bridge.replies["/send-media"] = (200, {"success": True, "messageId": "MEDIA1"})
    png = b"\x89PNG\r\n\x1a\nfake"

    r = client.post(
        f"{PREFIX}/conversations/{a}/reply-media",
        json={"filename": "../../evil photo.png", "mime": "image/png", "data_base64": b64(png), "caption": " look "},
    )

    assert r.status_code == 200
    ((port, method, path, payload),) = bridge.calls
    assert (port, method, path) == (account_port(db, account), "POST", "/send-media")
    assert set(payload) == {"chatId", "filePath", "caption", "fileName"}
    assert (payload["chatId"], payload["caption"], payload["fileName"]) == (CONTACT_JID, "look", "evil photo.png")
    saved = Path(payload["filePath"])
    uploads = core.db.data_dir() / "uploads" / str(account)
    assert saved.parent == uploads and saved.read_bytes() == png

    message = r.json()["message"]
    assert (message["status"], message["wa_id"], message["body"], message["author"]) == ("sent", "MEDIA1", "look", "user")
    (item,) = message["media"]
    assert (item["type"], item["mime"], item["name"], item["size"], item["available"]) == (
        "image", "image/png", "evil photo.png", len(png), True,
    )
    assert r.json()["summary"]["state"] == "waiting"
    data = client.get(f"{PREFIX}/messages/{message['id']}/media/0").json()
    assert data["data_url"] == "data:image/png;base64," + b64(png)


def test_reply_media_enforces_size_limit_and_valid_base64(client, db, account, bridge):
    a = add_conv(db, account, CONTACT_JID, "new")
    settings = client.get(f"{PREFIX}/settings").json()
    settings["media"]["max_upload_mb"] = 1
    assert client.put(f"{PREFIX}/settings", json=settings).status_code == 200
    body = {"filename": "big.bin", "mime": "application/octet-stream", "data_base64": b64(b"x" * (1024 * 1024 + 1))}
    assert client.post(f"{PREFIX}/conversations/{a}/reply-media", json=body).status_code == 413
    body["data_base64"] = b64(b"x" * 1024)
    bridge.replies["/send-media"] = (200, {"success": True, "messageId": "OK"})
    assert client.post(f"{PREFIX}/conversations/{a}/reply-media", json=body).status_code == 200
    body["data_base64"] = "not base64!!"
    assert client.post(f"{PREFIX}/conversations/{a}/reply-media", json=body).status_code == 400
    assert len(bridge.calls) == 1
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


def test_reply_media_failure_keeps_failed_message(client, db, account, bridge):
    a = add_conv(db, account, CONTACT_JID, "new")
    bridge.replies["/send-media"] = (500, {"error": "File not found"})
    body = {"filename": "a.txt", "mime": "text/plain", "data_base64": b64(b"hi")}
    assert client.post(f"{PREFIX}/conversations/{a}/reply-media", json=body).status_code == 502
    (message,) = messages(client, a)["messages"]
    assert message["status"] == "failed" and message["media"][0]["name"] == "a.txt"


# --- Drafts --------------------------------------------------------------------------


def test_draft_lifecycle_create_approve_with_edit_and_discard(client, db, account, bridge):
    a = add_conv(db, account, CONTACT_JID, "new", unread=1)
    add_msg(db, a, "in", "can I get a quote?", NOW - 60)

    r = client.post(f"{PREFIX}/conversations/{a}/drafts", json={"text": "  Sure, here it is  "})
    assert r.status_code == 200
    draft = r.json()
    assert (draft["author"], draft["status"], draft["body"], draft["direction"]) == ("user", "draft", "Sure, here it is", "out")
    card = columns(client)["new"][0]
    assert card["has_draft"] is True
    assert card["last_message_preview"] == "can I get a quote?"  # a draft is not a message yet
    assert any(m["id"] == draft["id"] for m in messages(client, a)["messages"])
    assert "message.draft" in event_types(db)
    assert bridge.calls == []

    bridge.replies["/send"] = (200, {"success": True, "messageId": "SENT1"})
    r = client.post(f"{PREFIX}/messages/{draft['id']}/approve", json={"text": "Sure, edited"})
    assert r.status_code == 200
    sent = r.json()
    assert (sent["id"], sent["status"], sent["body"], sent["wa_id"]) == (draft["id"], "sent", "Sure, edited", "SENT1")
    assert bridge.calls[-1][3] == {"chatId": CONTACT_JID, "message": "Sure, edited"}
    row = conv_row(db, a)
    assert (row["state"], row["unread_count"]) == ("waiting", 0)
    assert columns(client)["waiting"][0]["has_draft"] is False

    second = client.post(f"{PREFIX}/conversations/{a}/drafts", json={"text": "never mind"}).json()
    r = client.post(f"{PREFIX}/messages/{second['id']}/discard")
    assert (r.status_code, r.json()["status"]) == (200, "discarded")
    assert all(m["id"] != second["id"] for m in messages(client, a)["messages"])
    assert columns(client)["waiting"][0]["has_draft"] is False


def test_approve_without_body_sends_the_draft_text(client, db, account, bridge):
    a = add_conv(db, account, CONTACT_JID, "new")
    draft = client.post(f"{PREFIX}/conversations/{a}/drafts", json={"text": "as written"}).json()
    bridge.replies["/send"] = (200, {"success": True, "messageId": "S"})
    assert client.post(f"{PREFIX}/messages/{draft['id']}/approve").json()["body"] == "as written"


def test_draft_errors_409_404_400_and_unavailable_keeps_draft(client, core, db, account, bridge):
    a = add_conv(db, account, CONTACT_JID, "new")
    assert client.post(f"{PREFIX}/conversations/{a}/drafts", json={"text": "  "}).status_code == 400
    assert client.post(f"{PREFIX}/conversations/9999/drafts", json={"text": "x"}).status_code == 404
    assert client.post(f"{PREFIX}/messages/9999/approve").status_code == 404
    assert client.post(f"{PREFIX}/messages/9999/discard").status_code == 404

    draft = client.post(f"{PREFIX}/conversations/{a}/drafts", json={"text": "hello"}).json()
    assert client.post(f"{PREFIX}/messages/{draft['id']}/approve", json={"text": "  "}).status_code == 400
    # bridge down: sending fails, and the failure is a failed message, not a lost draft
    assert client.post(f"{PREFIX}/messages/{draft['id']}/approve").status_code == 503
    assert [m["status"] for m in messages(client, a)["messages"]] == ["failed"]
    assert client.post(f"{PREFIX}/messages/{draft['id']}/approve").status_code == 409
    assert client.post(f"{PREFIX}/messages/{draft['id']}/discard").status_code == 409

    stopped = add_account(core, db, "Off", desired="stopped")
    b = add_conv(db, stopped, CONTACT_JID, "new")
    waiting = core.outbound.create_draft(db, b, "queued", author="agent:default", now=NOW, rule_id=7)
    assert client.post(f"{PREFIX}/messages/{waiting['id']}/approve").status_code == 503
    assert (waiting["author"], waiting["rule_id"]) == ("agent:default", 7)
    assert [m["status"] for m in messages(client, b)["messages"]] == ["draft"]


# --- Settings ------------------------------------------------------------------------

DEFAULT_SETTINGS = {
    "rules": {
        "inbound_on_closed": "reopen_new",
        "inbound_on_waiting": "in_progress",
        "inbound_on_muted": "reopen_new",
        "outbound_on_active": "waiting",
        "auto_reply_window_seconds": 10,
        "auto_close_waiting_days": 7,
    },
    "notifications": {"new_conversation": True, "every_inbound": False, "escalation": True, "quiet_hours": None},
    "board": {"urgency_hours": 24, "mute_presets_hours": [1, 8, 24, 168], "drop_mute_hours": 24, "closed_limit": 50},
    "hours": {
        "timezone": "Europe/Rome",
        "business": {
            "mon": [["09:00", "18:00"]],
            "tue": [["09:00", "18:00"]],
            "wed": [["09:00", "18:00"]],
            "thu": [["09:00", "18:00"]],
            "fri": [["09:00", "18:00"]],
            "sat": [],
            "sun": [],
        },
    },
    "automations": {"enabled": True, "max_runs_per_conversation_per_hour": 10, "history_messages_in_prompt": 20},
    "media": {"max_upload_mb": 15},
}


def test_settings_defaults(client):
    assert client.get(f"{PREFIX}/settings").json() == DEFAULT_SETTINGS


def test_settings_round_trip_and_event(client, db):
    settings = client.get(f"{PREFIX}/settings").json()
    settings["rules"]["inbound_on_closed"] = "reopen_previous"
    settings["rules"]["auto_close_waiting_days"] = None
    settings["notifications"]["quiet_hours"] = {"start": "22:00", "end": "08:00"}
    settings["board"]["mute_presets_hours"] = [2, 12]
    settings["hours"]["business"]["sat"] = [["10:00", "13:00"]]
    r = client.put(f"{PREFIX}/settings", json=settings)
    assert r.status_code == 200
    assert r.json() == settings
    assert client.get(f"{PREFIX}/settings").json() == settings
    assert "settings.updated" in event_types(db)


def test_put_settings_replaces_the_whole_object(client):
    r = client.put(f"{PREFIX}/settings", json={"rules": {"auto_reply_window_seconds": 30}})
    assert r.status_code == 200
    assert r.json()["rules"]["auto_reply_window_seconds"] == 30
    assert r.json()["rules"]["inbound_on_closed"] == "reopen_new"
    assert r.json()["board"] == DEFAULT_SETTINGS["board"]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s["rules"].update(inbound_on_closed="nope"),
        lambda s: s["rules"].update(auto_reply_window_seconds=301),
        lambda s: s["rules"].update(auto_close_waiting_days=0),
        lambda s: s["rules"].update(auto_close_waiting_days=366),
        lambda s: s["rules"].update(unknown_rule=True),
        lambda s: s["notifications"].update(quiet_hours={"start": "25:00", "end": "08:00"}),
        lambda s: s["board"].update(closed_limit=0),
        lambda s: s["hours"].update(timezone="Mars/Olympus"),
        lambda s: s["hours"]["business"].update(mon=[["18:00", "09:00"]]),
        lambda s: s["hours"]["business"].update(funday=[["09:00", "10:00"]]),
        lambda s: s["media"].update(max_upload_mb=0),
    ],
)
def test_put_settings_validation_422(client, mutate):
    settings = client.get(f"{PREFIX}/settings").json()
    mutate(settings)
    r = client.put(f"{PREFIX}/settings", json=settings)
    assert r.status_code == 422
    assert isinstance(r.json()["detail"], str)
    assert client.get(f"{PREFIX}/settings").json() == DEFAULT_SETTINGS


# --- Accounts ------------------------------------------------------------------------


def test_create_account_allocates_ports_skipping_used_ones(client, core, db):
    r = client.post(f"{PREFIX}/accounts", json={"label": " Sales ", "color": "purple", "history_mode": "full"})
    assert r.status_code == 200
    first = r.json()
    assert first["port"] == 3017
    assert (first["kind"], first["label"], first["color"], first["history_mode"]) == ("whatsapp", "Sales", "purple", "full")
    assert (first["desired"], first["paired"], first["phone"], first["hermes_profile"]) == ("running", False, None, None)
    assert first["status"]["state"] == "starting"
    assert first["status"]["heartbeat_at"] is None
    assert first["created_at"] >= NOW

    add_account(core, db, "Raw", port=3018)
    second = client.post(f"{PREFIX}/accounts", json={"label": "Support"}).json()
    assert (second["port"], second["color"], second["history_mode"]) == (3019, "blue", "recent")
    assert core.accounts.session_path(second) == core.db.data_dir() / f"sessions/{second['id']}"
    assert core.accounts.media_dir(second) == core.db.data_dir() / "wa-media" / str(second["id"])
    assert "account.status" in event_types(db)
    assert [a["label"] for a in client.get(f"{PREFIX}/accounts").json()["accounts"]] == ["Sales", "Raw", "Support"]


def test_create_account_skips_every_used_port(client, core, db):
    for port in range(3017, 3030):
        add_account(core, db, f"p{port}", port=port)
    assert client.post(f"{PREFIX}/accounts", json={"label": "Next"}).json()["port"] == 3030


def test_create_and_patch_account_validation(client):
    assert client.post(f"{PREFIX}/accounts", json={"label": "  "}).status_code == 400
    assert client.post(f"{PREFIX}/accounts", json={"label": "x", "history_mode": "forever"}).status_code == 400
    account = client.post(f"{PREFIX}/accounts", json={"label": "Sales"}).json()
    url = f"{PREFIX}/accounts/{account['id']}"
    r = client.patch(url, json={"label": "Sales EU", "color": "red", "history_mode": "off", "hermes_profile": "support"})
    assert r.status_code == 200
    assert (r.json()["label"], r.json()["color"], r.json()["history_mode"], r.json()["hermes_profile"]) == (
        "Sales EU", "red", "off", "support",
    )
    assert client.patch(url, json={"label": "Only label"}).json()["hermes_profile"] == "support"
    assert client.patch(url, json={"hermes_profile": ""}).json()["hermes_profile"] is None
    assert client.patch(url, json={"label": " "}).status_code == 400
    assert client.patch(url, json={"history_mode": "x"}).status_code == 400
    assert client.patch(f"{PREFIX}/accounts/999", json={"label": "x"}).status_code == 404


def test_account_desired_state_actions(client, core, db):
    account = client.post(f"{PREFIX}/accounts", json={"label": "Sales"}).json()
    base = f"{PREFIX}/accounts/{account['id']}"
    assert client.post(f"{base}/stop").json()["desired"] == "stopped"
    assert client.post(f"{base}/start").json()["desired"] == "running"
    assert client.post(f"{base}/logout").json()["desired"] == "logged_out"
    restarted = client.post(f"{base}/restart").json()
    assert restarted["desired"] == "running"
    assert restarted["restart_requested_at"] >= NOW
    assert client.post(f"{PREFIX}/accounts/999/stop").status_code == 404

    demo = add_account(core, db, "Demo", kind="demo", color="gray")
    assert client.post(f"{PREFIX}/accounts/{demo}/stop").status_code == 400
    assert client.post(f"{PREFIX}/accounts/{demo}/restart").status_code == 400


def test_remove_whatsapp_account_waits_for_sidecar_and_can_keep_or_delete_conversations(client, core, db):
    keep = add_account(core, db, "Keep")
    drop = add_account(core, db, "Drop")
    kept = add_conv(db, keep, "a@s.whatsapp.net", "new")
    dropped = add_conv(db, drop, "b@s.whatsapp.net", "new")
    add_msg(db, kept, "in", "k", NOW)
    add_msg(db, dropped, "in", "d", NOW)

    assert client.delete(f"{PREFIX}/accounts/{keep}").json() == {"ok": True}
    assert client.delete(f"{PREFIX}/accounts/{drop}", params={"delete_conversations": "true"}).json() == {"ok": True}
    assert client.get(f"{PREFIX}/accounts").json() == {"accounts": []}
    rows = {r["id"]: r for r in db.execute("SELECT * FROM accounts")}
    assert (rows[keep]["desired"], rows[keep]["delete_conversations"]) == ("removed", 0)
    assert (rows[drop]["desired"], rows[drop]["delete_conversations"]) == ("removed", 1)
    assert client.delete(f"{PREFIX}/accounts/999").status_code == 404

    core.accounts.finalize_removed(db, keep)
    core.accounts.finalize_removed(db, drop)
    assert db.execute("SELECT COUNT(*) FROM accounts").fetchone()[0] == 0
    assert [r[0] for r in db.execute("SELECT id FROM conversations")] == [kept]
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    card = client.get(f"{PREFIX}/conversations").json()["conversations"][0]
    assert (card["account_label"], card["account_color"]) == ("Removed account", "gray")


def test_remove_demo_account_is_immediate_and_deletes_its_data(client, core, db, account):
    demo = add_account(core, db, "Demo", kind="demo", color="gray")
    real = add_conv(db, account, CONTACT_JID, "new")
    fake = add_conv(db, demo, "5551@demo.invalid", "new")
    add_msg(db, real, "in", "real", NOW)
    add_msg(db, fake, "in", "fake", NOW)
    core.ingest.ingest_event(db, account, wa_event(messageId="E1", chatId="2@s.whatsapp.net", senderId="2@s.whatsapp.net"), NOW)

    assert client.delete(f"{PREFIX}/accounts/{demo}").json() == {"ok": True}
    assert [a["label"] for a in client.get(f"{PREFIX}/accounts").json()["accounts"]] == ["Main"]
    assert db.execute("SELECT COUNT(*) FROM accounts WHERE kind = 'demo'").fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM conversations WHERE id = ?", (fake,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM messages WHERE conversation_id = ?", (fake,)).fetchone()[0] == 0
    assert db.execute("SELECT COUNT(*) FROM conversations WHERE id = ?", (real,)).fetchone()[0] == 1


def test_sidecar_status_reports_emit_account_events_and_keep_qr(client, core, db):
    account = client.post(f"{PREFIX}/accounts", json={"label": "Sales"}).json()
    aid = account["id"]
    base_events = event_types(db).count("account.status")

    core.accounts.set_status(db, aid, state="qr", now=NOW, qr="2@qrdata", qr_svg="<svg/>", pid=77)
    (listed,) = client.get(f"{PREFIX}/accounts").json()["accounts"]
    assert listed["status"]["state"] == "qr"
    assert listed["status"]["qr_svg"] == "<svg/>"
    assert listed["status"]["qr_at"] == NOW
    assert listed["status"]["heartbeat_at"] == NOW
    assert event_types(db).count("account.status") == base_events + 1

    # heartbeat-only report: no new event, QR kept while still in the qr state
    core.accounts.set_status(db, aid, state="qr", now=NOW + 5)
    assert event_types(db).count("account.status") == base_events + 1
    status = core.accounts.get_account(db, aid)["status"]
    assert (status["qr_svg"], status["heartbeat_at"]) == ("<svg/>", NOW + 5)

    core.accounts.set_status(db, aid, state="connected", now=NOW + 10)
    status = core.accounts.get_account(db, aid)["status"]
    assert (status["state"], status["qr_svg"], status["error"]) == ("connected", None, None)
    assert event_payloads(db, "account.status")[-1] == {"state": "connected"}

    core.accounts.set_status(db, aid, state="error", now=NOW + 11, error="boom")
    assert core.accounts.get_account(db, aid)["status"]["error"] == "boom"


def test_record_paired_stores_phone_and_name(client, core, db):
    account = client.post(f"{PREFIX}/accounts", json={"label": "Sales"}).json()
    core.accounts.record_paired(db, account["id"], wa_jid="393330001111:12@s.whatsapp.net", wa_name="Shop", now=NOW)
    paired = core.accounts.get_account(db, account["id"])
    assert (paired["phone"], paired["wa_name"], paired["wa_jid"]) == ("393330001111", "Shop", "393330001111:12@s.whatsapp.net")
    assert paired["paired"] is False  # paired means the session credentials exist on disk
    session = core.accounts.session_path(paired)
    session.mkdir(parents=True)
    (session / "creds.json").write_text("{}", encoding="utf-8")
    assert client.get(f"{PREFIX}/accounts").json()["accounts"][0]["paired"] is True


# --- WS /events ----------------------------------------------------------------


def test_events_stream_pushes_state_changes(client, core, db, account, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_EVENT_POLL_SECONDS", 0.05)
    a = add_conv(db, account, "a@s.whatsapp.net", "new", name="Anna")
    with client.websocket_connect(f"{PREFIX}/events") as ws:
        assert post(client, a, "state", state="in_progress").status_code == 200
        frame = ws.receive_json()
    (event,) = [e for e in frame["events"] if e["type"] == "conversation.state_changed"]
    assert (event["conversation_id"], event["account_id"], event["contact_name"]) == (a, account, "Anna")
    assert event["payload"]["to"] == "in_progress"
    assert frame["cursor"] == frame["events"][-1]["id"]


def test_events_stream_replays_from_since_and_starts_at_tail_without_it(client, core, db, account, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_EVENT_POLL_SECONDS", 0.05)
    core.ingest.ingest_event(db, account, wa_event(), NOW)
    with client.websocket_connect(f"{PREFIX}/events?since=0") as ws:
        frame = ws.receive_json()
    assert {"conversation.created", "message.in"} <= {e["type"] for e in frame["events"]}

    conv = conv_by_jid(db, account, CONTACT_JID)["id"]
    with client.websocket_connect(f"{PREFIX}/events") as ws:
        post(client, conv, "read")
        frame = ws.receive_json()
    assert [e["type"] for e in frame["events"]] == ["conversation.updated"]


def test_events_stream_rejects_unauthorized_upgrade(client, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_ws_upgrade_authorized", lambda ws: False)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(f"{PREFIX}/events"):
            pass
    assert exc.value.code == 1008


# --- Demo seed -----------------------------------------------------------------


def test_seeded_400_conversations_board_is_bounded_and_fast(client, db, seed_mod):
    counts = seed_mod.seed(db, 400, NOW)
    assert counts == {"new": 8, "in_progress": 9, "waiting": 7, "muted": 4, "closed": 372}
    cols = columns(client)
    assert sum(len(cards) for name, cards in cols.items() if name != "closed") == 28
    assert cols["closed"] == []
    assert {c["account_label"] for cards in cols.values() for c in cards} == {"Demo"}

    timings = []
    for _ in range(20):
        start = time.perf_counter()
        assert client.get(f"{PREFIX}/board").status_code == 200
        timings.append(time.perf_counter() - start)
    assert sorted(timings)[18] < 0.3

    start = time.perf_counter()
    assert client.get(f"{PREFIX}/conversations", params={"limit": 50}).json()["total"] == 400
    assert time.perf_counter() - start < 0.3

    with pytest.raises(ValueError):
        seed_mod.seed(db, 27, NOW)


def test_seed_goes_into_a_demo_account_and_remove_keeps_real_data(client, core, db, account, seed_mod):
    real = add_conv(db, account, CONTACT_JID, "new")
    add_msg(db, real, "in", "real", NOW)
    seed_mod.seed(db, 40, NOW)
    (demo,) = [a for a in client.get(f"{PREFIX}/accounts").json()["accounts"] if a["kind"] == "demo"]
    assert demo["label"] == "Demo"
    assert db.execute("SELECT COUNT(*) FROM conversations WHERE account_id = ?", (demo["id"],)).fetchone()[0] == 40

    removed = seed_mod.remove_demo(db)
    assert removed == 40
    assert [r[0] for r in db.execute("SELECT id FROM conversations")] == [real]
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM conversation_state_log").fetchone()[0] == 0
    assert [a["label"] for a in client.get(f"{PREFIX}/accounts").json()["accounts"]] == ["Main"]
