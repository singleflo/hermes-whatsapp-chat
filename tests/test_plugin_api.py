"""Backend tests for hermes-whatsapp-chat.

Loads the REAL plugin/dashboard/plugin_api.py, mounts its router in a bare
FastAPI app and drives it over HTTP against a throwaway SQLite DB. Only the
WhatsApp bridge (an external process) is faked.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = REPO_ROOT / "plugin" / "dashboard" / "plugin_api.py"
SEED_FILE = REPO_ROOT / "scripts" / "seed_demo.py"
PREFIX = "/api/plugins/hermes-whatsapp-chat"
CONTACT_JID = "393330001111@s.whatsapp.net"
OWN_JID = "393990000000@s.whatsapp.net"


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
    monkeypatch.setenv("WA_ARCHIVE_DB", str(tmp_path / "wa_board.db"))
    return _load("hermes_dashboard_plugin_hermes_whatsapp_chat", PLUGIN_FILE)


@pytest.fixture(autouse=True)
def bridge(plugin, monkeypatch):
    calls: list[tuple] = []
    replies: dict = {}

    def fake(method, path, payload=None, *, timeout):
        calls.append((method, path, payload))
        if path not in replies:
            raise plugin.BridgeUnavailable("no bridge in tests")
        reply = replies[path]
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(plugin, "_bridge_request", fake)
    return SimpleNamespace(calls=calls, replies=replies)


@pytest.fixture
def client(plugin):
    app = FastAPI()
    app.include_router(plugin.router, prefix=PREFIX)
    return TestClient(app)


@pytest.fixture
def db(plugin):
    conn = plugin._conn()
    yield conn
    conn.close()


@pytest.fixture
def seed_mod():
    return _load("hwc_seed_demo", SEED_FILE)


# --- Helpers -------------------------------------------------------------------


def add_conv(
    db,
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
):
    last_at = last_at if last_at is not None else int(time.time()) - 60
    db.execute(
        "INSERT INTO conversations (chat_jid, contact_name, phone, state, priority, muted_until,"
        " last_message_at, unread_count, agent_active, tags, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (
            jid,
            name,
            jid.split("@")[0],
            state,
            priority,
            muted_until,
            last_at,
            unread,
            agent_active,
            json.dumps(tags or []),
            last_at,
        ),
    )


def add_msg(db, jid, direction, body, ts):
    db.execute(
        "INSERT INTO messages (chat_jid, direction, body, ts) VALUES (?,?,?,?)",
        (jid, direction, body, ts),
    )


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
        "timestamp": int(time.time()),
    }
    event.update(overrides)
    return event


def columns(client, **params):
    body = client.get(f"{PREFIX}/board", params=params).json()
    return {c["name"]: c["cards"] for c in body["columns"]}


def post(client, jid, action, **body):
    return client.post(f"{PREFIX}/chats/{jid}/{action}", json=body)


def conv_row(db, jid):
    return db.execute("SELECT * FROM conversations WHERE chat_jid=?", (jid,)).fetchone()


def log_rows(db, jid):
    return db.execute(
        "SELECT * FROM conversation_state_log WHERE chat_jid=? ORDER BY id", (jid,)
    ).fetchall()


# --- GET /health ---------------------------------------------------------------


def test_health_reports_db_and_count(client, db):
    add_conv(db, "a@s.whatsapp.net", "new")
    add_conv(db, "b@s.whatsapp.net", "closed")
    body = client.get(f"{PREFIX}/health").json()
    assert body["ok"] is True
    assert body["conversations"] == 2
    assert body["db"].endswith("wa_board.db")


# --- GET /board ----------------------------------------------------------------


def test_empty_db_returns_five_empty_columns(client):
    body = client.get(f"{PREFIX}/board").json()
    assert [c["name"] for c in body["columns"]] == ["new", "in_progress", "waiting", "muted", "closed"]
    assert all(c["cards"] == [] for c in body["columns"])


def test_board_groups_cards_and_builds_payload(client, db):
    now = int(time.time())
    add_conv(db, "a@s.whatsapp.net", "new", name="Anna", last_at=now - 120, unread=2, tags=["case"])
    add_msg(db, "a@s.whatsapp.net", "out", "older", now - 500)
    add_msg(db, "a@s.whatsapp.net", "in", "where is my order?", now - 120)
    add_conv(db, "b@s.whatsapp.net", "waiting", name="Bruno")

    cols = columns(client)
    assert [c["contact_name"] for c in cols["new"]] == ["Anna"]
    assert [c["contact_name"] for c in cols["waiting"]] == ["Bruno"]
    card = cols["new"][0]
    assert card["chat_jid"] == "a@s.whatsapp.net"
    assert card["state"] == "new"
    assert card["last_message_preview"] == "where is my order?"
    assert 119 <= card["age_seconds"] <= 130
    assert card["unread_count"] == 2
    assert card["agent_active"] is True
    assert card["tags"] == ["case"]
    assert card["urgency"] == "normal"


def test_board_preview_truncated_to_200_chars(client, db):
    add_conv(db, "a@s.whatsapp.net", "new")
    add_msg(db, "a@s.whatsapp.net", "in", "x" * 500, int(time.time()) - 60)
    assert len(columns(client)["new"][0]["last_message_preview"]) == 200


def test_unknown_state_lands_in_fallback_column_and_can_move(client, db):
    add_conv(db, "a@s.whatsapp.net", "weird")
    card = columns(client)["in_progress"][0]
    assert card["state"] == "weird"
    assert post(client, "a@s.whatsapp.net", "state", state="waiting").status_code == 200
    assert columns(client)["waiting"][0]["chat_jid"] == "a@s.whatsapp.net"


def test_board_excludes_closed_unless_requested(client, db):
    add_conv(db, "a@s.whatsapp.net", "closed", name="Done")
    assert columns(client)["closed"] == []
    shown = columns(client, include_closed="true")["closed"]
    assert [c["contact_name"] for c in shown] == ["Done"]


def test_board_search_filters_by_name_or_phone(client, db):
    add_conv(db, "111@s.whatsapp.net", "new", name="Alice Martin")
    add_conv(db, "222@s.whatsapp.net", "new", name="Bob Stone")
    assert [c["contact_name"] for c in columns(client, q="alice")["new"]] == ["Alice Martin"]
    assert [c["contact_name"] for c in columns(client, q="222")["new"]] == ["Bob Stone"]
    assert len(columns(client, q="  ")["new"]) == 2


def test_escalated_card_sorted_first_with_high_urgency(client, db):
    now = int(time.time())
    add_conv(db, "old@s.whatsapp.net", "new", last_at=now - 600)
    add_conv(db, "hot@s.whatsapp.net", "new", last_at=now - 60, priority=2)
    cards = columns(client)["new"]
    assert [c["chat_jid"] for c in cards] == ["hot@s.whatsapp.net", "old@s.whatsapp.net"]
    assert cards[0]["urgency"] == "high"


def test_stale_new_card_has_medium_urgency(client, db):
    add_conv(db, "a@s.whatsapp.net", "new", last_at=int(time.time()) - 2 * 86400)
    add_conv(db, "b@s.whatsapp.net", "in_progress", last_at=int(time.time()) - 2 * 86400)
    assert columns(client)["new"][0]["urgency"] == "medium"
    assert columns(client)["in_progress"][0]["urgency"] == "normal"


# --- State routes --------------------------------------------------------------


def test_valid_transition_persists_and_logs_server_actor(client, db):
    add_conv(db, "a@s.whatsapp.net", "new")
    r = post(client, "a@s.whatsapp.net", "state", state="in_progress", reason="on it", actor="agent")
    assert r.status_code == 200
    assert r.json()["state"] == "in_progress"
    assert conv_row(db, "a@s.whatsapp.net")["state"] == "in_progress"
    (entry,) = log_rows(db, "a@s.whatsapp.net")
    assert (entry["from_state"], entry["to_state"], entry["actor"], entry["reason"]) == (
        "new",
        "in_progress",
        "user",
        "on it",
    )


def test_invalid_transition_409_names_current_state(client, db):
    add_conv(db, "a@s.whatsapp.net", "waiting")
    r = post(client, "a@s.whatsapp.net", "state", state="new")
    assert r.status_code == 409
    assert "'waiting'" in r.json()["detail"]
    assert "in_progress" in r.json()["detail"]
    assert conv_row(db, "a@s.whatsapp.net")["state"] == "waiting"
    assert log_rows(db, "a@s.whatsapp.net") == []


def test_unknown_chat_404(client):
    assert post(client, "nope@s.whatsapp.net", "state", state="closed").status_code == 404
    assert client.get(f"{PREFIX}/chats/nope@s.whatsapp.net").status_code == 404


def test_unknown_target_state_400(client, db):
    add_conv(db, "a@s.whatsapp.net", "new")
    r = post(client, "a@s.whatsapp.net", "state", state="bogus")
    assert r.status_code == 400
    assert "bogus" in r.json()["detail"]


def test_mute_requires_future_timestamp(client, db):
    add_conv(db, "a@s.whatsapp.net", "new")
    assert post(client, "a@s.whatsapp.net", "mute", muted_until=int(time.time()) - 5).status_code == 400
    until = int(time.time()) + 3600
    r = post(client, "a@s.whatsapp.net", "mute", muted_until=until)
    assert r.status_code == 200
    row = conv_row(db, "a@s.whatsapp.net")
    assert (row["state"], row["muted_until"]) == ("muted", until)


def test_expired_mute_reopens_as_new_on_board_read(client, db):
    add_conv(db, "a@s.whatsapp.net", "muted", muted_until=int(time.time()) - 10)
    assert [c["chat_jid"] for c in columns(client)["new"]] == ["a@s.whatsapp.net"]
    row = conv_row(db, "a@s.whatsapp.net")
    assert row["muted_until"] is None
    (entry,) = log_rows(db, "a@s.whatsapp.net")
    assert (entry["from_state"], entry["to_state"], entry["actor"]) == ("muted", "new", "auto")


def test_takeover_moves_to_in_progress_and_disables_agent(client, db):
    add_conv(db, "a@s.whatsapp.net", "new")
    add_conv(db, "b@s.whatsapp.net", "closed")
    add_conv(db, "c@s.whatsapp.net", "in_progress")
    for jid in ("a", "b", "c"):
        r = post(client, f"{jid}@s.whatsapp.net", "takeover")
        assert r.status_code == 200
        assert (r.json()["state"], r.json()["agent_active"]) == ("in_progress", False)
    # already in progress: no state change, but the takeover is still audited
    (entry,) = log_rows(db, "c@s.whatsapp.net")
    assert (entry["from_state"], entry["to_state"], entry["reason"]) == ("in_progress", "in_progress", "takeover")
    # repeating is a no-op
    post(client, "c@s.whatsapp.net", "takeover")
    assert len(log_rows(db, "c@s.whatsapp.net")) == 1


def test_handback_reenables_agent(client, db):
    add_conv(db, "a@s.whatsapp.net", "in_progress", agent_active=0)
    assert post(client, "a@s.whatsapp.net", "handback").json()["agent_active"] is True
    post(client, "a@s.whatsapp.net", "handback")
    (entry,) = log_rows(db, "a@s.whatsapp.net")
    assert entry["reason"] == "handback"


def test_escalate_and_deescalate_set_priority(client, db):
    add_conv(db, "a@s.whatsapp.net", "new")
    assert post(client, "a@s.whatsapp.net", "escalate", escalated=True).json()["priority"] == 2
    assert post(client, "a@s.whatsapp.net", "escalate", escalated=False).json()["priority"] == 0
    assert [e["reason"] for e in log_rows(db, "a@s.whatsapp.net")] == ["escalated", "de-escalated"]


def test_mark_read_clears_unread(client, db):
    add_conv(db, "a@s.whatsapp.net", "new", unread=3)
    assert post(client, "a@s.whatsapp.net", "read").json()["unread_count"] == 0
    assert log_rows(db, "a@s.whatsapp.net") == []


# --- GET /chats/{jid} and /stats -------------------------------------------------


def test_chat_detail_lists_allowed_states_and_history(client, db):
    now = int(time.time())
    add_conv(db, "a@s.whatsapp.net", "new", name="Anna", tags=["info"])
    add_msg(db, "a@s.whatsapp.net", "in", "first", now - 100)
    add_msg(db, "a@s.whatsapp.net", "out", "second", now - 50)
    post(client, "a@s.whatsapp.net", "state", state="in_progress")

    body = client.get(f"{PREFIX}/chats/a@s.whatsapp.net").json()
    assert body["conversation"]["state"] == "in_progress"
    assert body["conversation"]["tags"] == ["info"]
    assert body["conversation"]["agent_active"] is True
    assert [m["body"] for m in body["messages"]] == ["first", "second"]
    assert body["allowed_states"] == ["closed", "muted", "waiting"]
    assert body["state_history"][0]["to_state"] == "in_progress"


def test_stats_counts_and_oldest_unanswered_age(client, db):
    now = int(time.time())
    add_conv(db, "a@s.whatsapp.net", "new", last_at=now - 3600)
    add_msg(db, "a@s.whatsapp.net", "in", "hi", now - 3600)
    add_conv(db, "b@s.whatsapp.net", "new", last_at=now - 100)
    add_msg(db, "b@s.whatsapp.net", "in", "hi", now - 100)
    add_conv(db, "c@s.whatsapp.net", "in_progress", last_at=now - 7200)
    add_msg(db, "c@s.whatsapp.net", "out", "we reply", now - 7200)
    add_conv(db, "d@s.whatsapp.net", "weird")

    body = client.get(f"{PREFIX}/stats").json()
    assert body["counts"]["new"] == 2
    assert body["counts"]["in_progress"] == 1
    assert body["counts"]["closed"] == 0
    assert body["counts"]["weird"] == 1
    assert 3600 <= body["oldest_unanswered_age_seconds"] <= 3700



def test_stats_oldest_unanswered_is_none_without_inbound(client, db):
    add_conv(db, "c@s.whatsapp.net", "in_progress")
    add_msg(db, "c@s.whatsapp.net", "out", "we reply", int(time.time()) - 60)
    assert client.get(f"{PREFIX}/stats").json()["oldest_unanswered_age_seconds"] is None


# --- WS /events ----------------------------------------------------------------


def test_events_stream_pushes_state_changes(client, db, plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_EVENT_POLL_SECONDS", 0.05)
    add_conv(db, "a@s.whatsapp.net", "new", name="Anna")
    with client.websocket_connect(f"{PREFIX}/events?since=0") as ws:
        assert post(client, "a@s.whatsapp.net", "state", state="in_progress").status_code == 200
        frame = ws.receive_json()
    (event,) = frame["events"]
    assert (event["chat_jid"], event["to_state"], event["contact_name"]) == (
        "a@s.whatsapp.net",
        "in_progress",
        "Anna",
    )
    assert frame["cursor"] == event["id"]


# --- Ingest --------------------------------------------------------------------


def test_ingest_creates_conversation_and_dedups_by_wa_id(plugin, db):
    now = int(time.time())
    assert plugin.ingest_event(db, wa_event(), now) is True
    assert plugin.ingest_event(db, wa_event(), now) is False
    assert plugin.ingest_event(db, wa_event(messageId="G1", isGroup=True), now) is False

    row = conv_row(db, CONTACT_JID)
    assert (row["state"], row["unread_count"], row["contact_name"], row["phone"]) == (
        "new",
        1,
        "Mario",
        "393330001111",
    )
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    (entry,) = log_rows(db, CONTACT_JID)
    assert (entry["from_state"], entry["to_state"], entry["actor"]) == (None, "new", "auto")


def test_ingest_prefers_phone_jid_over_lid_and_skips_own_number(plugin, db):
    now = int(time.time())
    plugin.ingest_event(db, wa_event(messageId="L1", chatId="5551234@lid", senderId=CONTACT_JID), now)
    assert conv_row(db, CONTACT_JID) is not None
    assert conv_row(db, "5551234@lid") is None

    # typed on the dedicated phone: the sender is our own number, the chat is the contact
    plugin.ingest_event(
        db, wa_event(messageId="O1", chatId="393330002222@s.whatsapp.net", senderId=OWN_JID, fromOwner=True), now
    )
    assert conv_row(db, "393330002222@s.whatsapp.net") is not None
    assert conv_row(db, OWN_JID) is None


def test_ingest_parses_long_timestamp(plugin, db):
    now = int(time.time())
    plugin.ingest_event(db, wa_event(messageId="T1", timestamp={"low": now - 1000, "high": 0}), now)
    assert db.execute("SELECT ts FROM messages WHERE wa_id='T1'").fetchone()[0] == now - 1000
    # an absurd value falls back to "now"
    plugin.ingest_event(db, wa_event(messageId="T2", timestamp={"low": 5, "high": 9}), now)
    assert db.execute("SELECT ts FROM messages WHERE wa_id='T2'").fetchone()[0] == now


def test_ingest_reopens_muted_conversation_but_not_on_reaction(plugin, db):
    now = int(time.time())
    reacting = "393330003333@s.whatsapp.net"
    add_conv(db, CONTACT_JID, "muted", muted_until=now + 3600)
    add_conv(db, reacting, "muted", muted_until=now + 3600)

    plugin.ingest_event(db, wa_event(messageId="R1", chatId=reacting, senderId=reacting, body="👍", mediaType="reaction"), now)
    assert conv_row(db, reacting)["state"] == "muted"

    plugin.ingest_event(db, wa_event(messageId="R2"), now)
    row = conv_row(db, CONTACT_JID)
    assert (row["state"], row["muted_until"]) == ("new", None)
    entry = log_rows(db, CONTACT_JID)[-1]
    assert (entry["from_state"], entry["to_state"], entry["actor"]) == ("muted", "new", "auto")


def test_ingest_owner_messages_are_outbound(plugin, db):
    now = int(time.time())
    add_conv(db, CONTACT_JID, "in_progress", unread=2)
    plugin.ingest_event(db, wa_event(messageId="O1", senderId=OWN_JID, fromOwner=True, body="typed on phone"), now)
    assert db.execute("SELECT direction FROM messages WHERE wa_id='O1'").fetchone()[0] == "out"
    row = conv_row(db, CONTACT_JID)
    assert (row["unread_count"], row["state"]) == (0, "in_progress")

    fresh = "393330004444@s.whatsapp.net"
    plugin.ingest_event(
        db, wa_event(messageId="O2", chatId=fresh, senderId=OWN_JID, senderName="Me", fromOwner=True), now
    )
    row = conv_row(db, fresh)
    assert (row["state"], row["unread_count"], row["contact_name"]) == ("waiting", 0, None)


# --- POST /reply ---------------------------------------------------------------


def test_reply_sends_via_bridge_and_records_outbound(client, db, bridge):
    add_conv(db, CONTACT_JID, "new", unread=3)
    bridge.replies["/send"] = (200, {"success": True, "messageId": "ABC"})

    r = post(client, CONTACT_JID, "reply", text="  test reply  ")

    assert r.status_code == 200
    assert r.json()["message_id"] == "ABC"
    assert bridge.calls == [("POST", "/send", {"chatId": CONTACT_JID, "message": "test reply"})]
    msg = db.execute("SELECT * FROM messages WHERE chat_jid=?", (CONTACT_JID,)).fetchone()
    assert (msg["direction"], msg["wa_id"], msg["body"]) == ("out", "ABC", "test reply")
    assert conv_row(db, CONTACT_JID)["unread_count"] == 0


def test_reply_rejects_empty_text_demo_jid_and_unknown_chat(client, db, bridge):
    add_conv(db, "5551@demo.invalid", "new")
    assert post(client, CONTACT_JID, "reply", text="   ").status_code == 400
    assert post(client, "5551@demo.invalid", "reply", text="hi").status_code == 400
    assert post(client, CONTACT_JID, "reply", text="hi").status_code == 404
    assert bridge.calls == []


def test_reply_maps_bridge_failures(client, db, bridge):
    add_conv(db, CONTACT_JID, "new")
    assert post(client, CONTACT_JID, "reply", text="hi").status_code == 503  # bridge unreachable

    bridge.replies["/send"] = (503, {"error": "Not connected to WhatsApp"})
    r = post(client, CONTACT_JID, "reply", text="hi")
    assert r.status_code == 503
    assert "Not connected to WhatsApp" in r.json()["detail"]

    bridge.replies["/send"] = (500, {"error": "boom"})
    assert post(client, CONTACT_JID, "reply", text="hi").status_code == 502
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_stats_reports_channel_status(client, bridge):
    assert client.get(f"{PREFIX}/stats").json()["channel"] == "unreachable"
    bridge.replies["/health"] = (200, {"status": "connected"})
    assert client.get(f"{PREFIX}/stats").json()["channel"] == "connected"


# --- Demo seed -----------------------------------------------------------------


def test_seeded_400_conversations_board_is_bounded_and_fast(client, db, seed_mod):
    counts = seed_mod.seed(db, 400, int(time.time()))
    assert counts == {"new": 8, "in_progress": 9, "waiting": 7, "muted": 4, "closed": 372}
    cols = columns(client)
    assert sum(len(cards) for name, cards in cols.items() if name != "closed") == 28
    assert cols["closed"] == []

    timings = []
    for _ in range(20):
        start = time.perf_counter()
        assert client.get(f"{PREFIX}/board").status_code == 200
        timings.append(time.perf_counter() - start)
    assert sorted(timings)[18] < 0.3

    with pytest.raises(ValueError):
        seed_mod.seed(db, 27, int(time.time()))


def test_remove_demo_keeps_whatsapp_conversations(plugin, db, seed_mod):
    add_conv(db, CONTACT_JID, "new")
    add_msg(db, CONTACT_JID, "in", "real", int(time.time()))
    seed_mod.seed(db, 40, int(time.time()))

    assert seed_mod.remove_demo(db) == 40
    assert db.execute("SELECT chat_jid FROM conversations").fetchall()[0][0] == CONTACT_JID
    assert db.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    assert db.execute("SELECT COUNT(*) FROM conversation_state_log").fetchone()[0] == 0


# --- Degradation ---------------------------------------------------------------


def test_unopenable_db_returns_500_with_actionable_detail(client, plugin, tmp_path, monkeypatch):
    monkeypatch.setenv("WA_ARCHIVE_DB", str(tmp_path))  # a directory, not a database
    r = client.get(f"{PREFIX}/board")
    assert r.status_code == 500
    assert "board read failed" in r.json()["detail"]
    assert client.get(f"{PREFIX}/health").json()["ok"] is False
