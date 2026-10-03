"""Jev exit conditions for hermes-whatsapp-chat.

Loads the REAL plugin/dashboard/plugin_api.py (which loads wa_core) against a throwaway SQLite DB.
Faked boundaries only: the Jev HTTP call (``jev._post``), the ``hermes`` subprocess
(``automations._run_command``) and the WhatsApp bridge (``core.bridge.bridge_request``).
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = REPO_ROOT / "plugin" / "dashboard" / "plugin_api.py"
PREFIX = "/api/plugins/hermes-whatsapp-chat"
CONTACT_JID = "393330001111@s.whatsapp.net"
NOW = int(datetime(2026, 1, 14, 12, 0, tzinfo=ZoneInfo("Europe/Rome")).timestamp())
KEY = "k-test-123"

EXITS = [
    {"id": "person", "label": "Needs a person", "description": "The contact wants a person or complains", "min_score": 0.7},
    {"id": "agent", "label": "Agent can answer", "description": "A routine question an assistant can answer", "min_score": 0.7},
    {"id": "no_reply", "label": "No reply needed", "description": "Only a greeting or thanks", "min_score": 0.8},
]


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def answer(scores: dict[str, float], *, model: str = "jev-1.13.0", tokens: int = 321, headers: dict | None = None):
    """What the faked Jev returns: (status, text, headers)."""
    answers = {k: {"type": "noul", "noul": v, "confidence": max(v, 1 - v), "action": {"act_probability": 1.0}} for k, v in scores.items()}
    body = {"answers": answers, "model": model, "usage": {"input_tokens": tokens, "output_tokens": 0}}
    return 200, json.dumps(body), headers or {}


# --- Fixtures ----------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WA_ARCHIVE_DB", str(tmp_path / "wa_board.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    plugin = _load("hermes_dashboard_plugin_hermes_whatsapp_chat_jev", PLUGIN_FILE)
    core = plugin.core
    app = FastAPI()
    app.include_router(plugin.router, prefix=PREFIX)

    posts: list[dict] = []
    post_results: list = []
    commands: list[dict] = []

    def fake_post(body, key, timeout):
        posts.append({"body": json.loads(body), "key": key, "timeout": timeout})
        result = post_results.pop(0) if post_results else answer({"person": 0.1, "agent": 0.9, "no_reply": 0.1})
        if isinstance(result, Exception):
            raise result
        return result

    def fake_run(argv, *, stdin=None, timeout, env=None):
        commands.append({"argv": list(argv), "timeout": timeout})
        return 0, "Sure, we can help.\n", ""

    def fake_bridge(port, method, path, payload=None, *, timeout):
        return 200, {"success": True, "messageId": "WAID1"}

    real_post = core.jev._post
    monkeypatch.setattr(core.jev, "_post", fake_post)
    monkeypatch.setattr(core.automations, "_run_command", fake_run)
    monkeypatch.setattr(core.bridge, "bridge_request", fake_bridge)

    conn = core.db.connect()
    yield SimpleNamespace(
        core=core, client=TestClient(app), conn=conn, posts=posts,
        post_results=post_results,
        commands=commands,
        tmp_path=tmp_path,
        real_post=real_post,
    )
    conn.close()


# --- Seed helpers ----------------------------------------------------------------------------


def add_account(env, account_id: int = 1, label: str = "Main", kind: str = "whatsapp") -> int:
    env.conn.execute(
        "INSERT INTO accounts (id, kind, label, port, session_dir, desired, created_at) VALUES (?,?,?,?,?,'running',?)",
        (account_id, kind, label, 3016 + account_id, f"sessions/{account_id}", NOW),
    )
    return account_id


def add_conv(env, account_id: int = 1, jid: str = CONTACT_JID, name: str = "Anna", agent_active: int = 1) -> int:
    cur = env.conn.execute(
        "INSERT INTO conversations (account_id, chat_jid, contact_name, phone, state, agent_active, tags, created_at, updated_at)"
        " VALUES (?,?,?,?,'new',?,'[]',?,?)",
        (account_id, jid, name, jid.split("@")[0], agent_active, NOW, NOW),
    )
    return int(cur.lastrowid or 0)


def add_msg(
    env,
    conv_id: int,
    body: str | None = "a che punto è la mia pratica?",
    *,
    direction: str = "in",
    source: str = "live",
    ts: int = NOW,
    media_type: str | None = None,
    status: str | None = None,
) -> int:
    account_id = env.conn.execute("SELECT account_id FROM conversations WHERE id = ?", (conv_id,)).fetchone()[0]
    meta = json.dumps({"mediaType": media_type, "media": []}) if media_type else None
    cur = env.conn.execute(
        "INSERT INTO messages (conversation_id, account_id, direction, author, body, ts, status, source, meta)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        (
            conv_id,
            account_id,
            direction,
            "contact" if direction == "in" else "user",
            body,
            ts,
            status or ("received" if direction == "in" else "sent"),
            source,
            meta,
        ),
    )
    return int(cur.lastrowid or 0)


def fire(env, event_type: str, conv_id: int, message_id: int | None = None, now: int = NOW) -> int:
    account_id = env.conn.execute("SELECT account_id FROM conversations WHERE id = ?", (conv_id,)).fetchone()[0]
    with env.core.db.write_txn(env.conn):
        return env.core.events.emit(
            env.conn, event_type, now=now, account_id=account_id, conversation_id=conv_id, message_id=message_id
        )


def inbound(env, conv_id: int, body: str | None = "a che punto è la mia pratica?", *, now: int = NOW, **kw) -> int:
    msg_id = add_msg(env, conv_id, body, ts=now, **kw)
    fire(env, "message.in", conv_id, msg_id, now=now)
    return msg_id


def configure(env, *, key: str | None = KEY, **jev_fields) -> None:
    """Enable Jev with the three starter exits (overridable) and store the key."""
    cfg = env.core.settings.get_settings(env.conn).model_dump()
    cfg["jev"].update({"enabled": True, "exits": EXITS, **jev_fields})
    env.core.settings.Settings.model_validate(cfg)  # the stored object must be valid
    env.conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('global', ?, ?)", (json.dumps(cfg), NOW)
    )
    if key is not None:
        env.core.jev.set_api_key(env.conn, key, NOW)


def jev_runs(env) -> list[dict]:
    return [dict(r) for r in env.conn.execute("SELECT * FROM jev_runs ORDER BY id")]


def process(env, now: int = NOW + 5, limit: int = 4) -> int:
    return env.core.jev.process_due(env.core.db.connect, now, limit=limit)


def classification(env, conv_id: int) -> Any:
    raw = env.conn.execute("SELECT classification FROM conversations WHERE id = ?", (conv_id,)).fetchone()[0]
    return json.loads(raw) if raw else None


def event_rows(env, type_: str) -> list[dict]:
    return [dict(r) for r in env.conn.execute("SELECT * FROM events WHERE type = ? ORDER BY id", (type_,))]


def mk_rule(env, action: dict, **fields) -> dict:
    res = env.client.post(f"{PREFIX}/automations", json={"name": "rule", "action": action, **fields})
    assert res.status_code == 200, res.text
    return res.json()


def automation_runs(env) -> list[dict]:
    return [dict(r) for r in env.conn.execute("SELECT * FROM automation_runs ORDER BY id")]


@pytest.fixture
def conv(env):
    add_account(env)
    return add_conv(env)


# --- Schema migrations ---------------------------------------------------------------------------

V2_MESSAGES = """
CREATE TABLE messages (
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL, account_id INTEGER NOT NULL,
  wa_id TEXT, direction TEXT NOT NULL CHECK (direction IN ('in','out')),
  author TEXT NOT NULL, body TEXT, ts INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('received','pending','sent','failed','draft','discarded')),
  source TEXT NOT NULL DEFAULT 'live' CHECK (source IN ('live','history')),
  meta TEXT, error TEXT, rule_id INTEGER)
"""


def _old_db(env, monkeypatch, version: int) -> Path:
    """A DB the way an older release left it: v4 with the newer pieces removed."""
    path = env.tmp_path / f"v{version}.db"
    monkeypatch.setenv("WA_ARCHIVE_DB", str(path))
    conn = env.core.db.connect()
    conn.execute(
        "INSERT INTO accounts (id, kind, label, port, session_dir, created_at) VALUES (1,'whatsapp','Main',3017,'s',?)", (NOW,)
    )
    conn.execute(
        "INSERT INTO conversations (id, account_id, chat_jid, contact_name, state, created_at) VALUES (7,1,?,'Anna','new',?)",
        (CONTACT_JID, NOW),
    )
    conn.execute("DROP TABLE jev_runs")
    conn.execute("ALTER TABLE conversations DROP COLUMN classification")
    if version == 2:
        conn.execute("DROP TABLE messages")
        conn.execute(V2_MESSAGES)
        conn.execute(
            "INSERT INTO messages (conversation_id, account_id, direction, author, body, ts, status)"
            " VALUES (7,1,'in','contact','ciao',?,'received')",
            (NOW,),
        )
    else:
        conn.execute(
            "INSERT INTO messages (conversation_id, account_id, direction, author, body, ts, status)"
            " VALUES (7,1,'in','contact','ciao',?,'received')",
            (NOW,),
        )
    conn.execute(f"PRAGMA user_version = {version}")
    conn.close()
    return path


@pytest.mark.parametrize("version", [2, 3])
def test_old_schema_migrates_to_v4_keeping_rows_and_adding_jev(env, monkeypatch, version):
    _old_db(env, monkeypatch, version)
    conn = env.core.db.connect()
    try:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
        cols = [r[1] for r in conn.execute("PRAGMA table_info(conversations)")]
        assert "classification" in cols
        assert conn.execute("SELECT COUNT(*) FROM jev_runs").fetchone()[0] == 0
        assert conn.execute("SELECT contact_name, classification FROM conversations WHERE id = 7").fetchone()[:] == ("Anna", None)
        assert conn.execute("SELECT body FROM messages").fetchone()[0] == "ciao"
        indexes = {r[1] for r in conn.execute("PRAGMA index_list(jev_runs)")}
        assert {"idx_jev_runs_status", "idx_jev_runs_conv"} <= indexes
        # the migrated DB is fully usable: a run can be inserted and read back
        conn.execute("INSERT INTO jev_runs (conversation_id, account_id, status, created_at) VALUES (7,1,'queued',?)", (NOW,))
    finally:
        conn.close()


# --- Settings ---------------------------------------------------------------------------------


def test_jev_is_off_by_default(env):
    jev = env.client.get(f"{PREFIX}/settings").json()["jev"]
    assert jev["enabled"] is False and jev["exits"] == []
    assert (jev["model"], jev["history_messages"], jev["debounce_seconds"], jev["timeout_s"]) == ("jev-latest", 10, 5, 10)


def _put_settings(env, **jev_fields):
    cfg = env.client.get(f"{PREFIX}/settings").json()
    cfg["jev"].update(jev_fields)
    return env.client.put(f"{PREFIX}/settings", json=cfg)


@pytest.mark.parametrize(
    "exits",
    [
        [EXITS[0], EXITS[0]],  # duplicate ids
        [{**EXITS[0], "id": "else"}],  # reserved
        [{**EXITS[0], "min_score": 1.5}],
        [{**EXITS[0], "min_score": -0.1}],
        [{**EXITS[0], "id": "Bad Id"}],
        [{**EXITS[0], "label": ""}],
        [{**EXITS[0], "extra": 1}],
    ],
)
def test_invalid_exits_are_rejected(env, exits):
    assert _put_settings(env, exits=exits).status_code == 422


def test_valid_exits_round_trip_in_order(env):
    r = _put_settings(env, enabled=True, exits=EXITS, account_ids=[1])
    assert r.status_code == 200
    jev = env.client.get(f"{PREFIX}/settings").json()["jev"]
    assert [e["id"] for e in jev["exits"]] == ["person", "agent", "no_reply"]
    assert jev["enabled"] is True and jev["account_ids"] == [1]


# --- API key ----------------------------------------------------------------------------------


def test_key_is_stored_never_returned_and_removable(env, monkeypatch):
    assert env.client.get(f"{PREFIX}/jev").json() == {"key_set": False, "key_source": None}
    r = env.client.put(f"{PREFIX}/jev/key", json={"api_key": "  secret-key  "})
    assert r.json() == {"key_set": True, "key_source": "settings"}
    assert env.client.get(f"{PREFIX}/jev").json() == {"key_set": True, "key_source": "settings"}
    assert "secret-key" not in json.dumps(env.client.get(f"{PREFIX}/settings").json())
    assert "secret-key" not in json.dumps(env.client.get(f"{PREFIX}/jev").json())
    assert env.core.jev.api_key(env.conn) == ("secret-key", "settings")

    assert env.client.put(f"{PREFIX}/jev/key", json={"api_key": ""}).json() == {"key_set": False, "key_source": None}
    assert env.conn.execute("SELECT 1 FROM settings WHERE key = 'jev_api_key'").fetchone() is None


def test_key_falls_back_to_the_environment_and_the_stored_key_wins(env, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "env-key")
    assert env.client.get(f"{PREFIX}/jev").json() == {"key_set": True, "key_source": "env"}
    assert env.core.jev.api_key(env.conn) == ("env-key", "env")
    env.client.put(f"{PREFIX}/jev/key", json={"api_key": "ui-key"})
    assert env.core.jev.api_key(env.conn) == ("ui-key", "settings")
    env.client.put(f"{PREFIX}/jev/key", json={"api_key": ""})
    assert env.client.get(f"{PREFIX}/jev").json() == {"key_set": True, "key_source": "env"}


# --- Enqueue -------------------------------------------------------------------------------------


def test_live_inbound_text_queues_one_run_after_the_debounce(env, conv):
    configure(env)
    msg_id = inbound(env, conv)
    (run,) = jev_runs(env)
    assert (run["status"], run["conversation_id"], run["account_id"], run["message_id"]) == ("queued", conv, 1, msg_id)
    assert run["not_before"] == NOW + 5 and run["attempts"] == 0


def test_a_second_inbound_inside_the_window_replaces_the_pending_run(env, conv):
    configure(env)
    inbound(env, conv)
    second = inbound(env, conv, "e quando arriva?", now=NOW + 3)
    (run,) = jev_runs(env)
    assert (run["message_id"], run["not_before"]) == (second, NOW + 3 + 5)


def test_a_new_run_is_queued_while_the_previous_one_is_running(env, conv):
    configure(env)
    inbound(env, conv)
    env.conn.execute("UPDATE jev_runs SET status = 'running', started_at = ?", (NOW,))
    inbound(env, conv, "ancora io", now=NOW + 1)
    assert [r["status"] for r in jev_runs(env)] == ["running", "queued"]


@pytest.mark.parametrize(
    "case",
    ["history", "outbound", "reaction", "poll_update", "empty_body", "demo_account", "out_of_scope", "disabled", "no_key", "no_exits"],
)
def test_messages_that_must_not_reach_jev(env, conv, case):
    configure(env)
    msg: dict = {}
    if case == "history":
        msg["source"] = "history"
    elif case == "outbound":
        msg["direction"] = "out"
    elif case in ("reaction", "poll_update"):
        msg["media_type"] = case
    elif case == "empty_body":
        msg.update(body=None, media_type="image")
    elif case == "demo_account":
        env.conn.execute("UPDATE accounts SET kind = 'demo' WHERE id = 1")
    elif case == "out_of_scope":
        add_account(env, 2, "Other")
        configure(env, account_ids=[2])
    elif case == "disabled":
        configure(env, enabled=False)
    elif case == "no_key":
        env.core.jev.set_api_key(env.conn, "", NOW)
    elif case == "no_exits":
        configure(env, exits=[])
    inbound(env, conv, **msg)
    assert jev_runs(env) == []


def test_a_number_in_scope_is_classified(env, conv):
    add_account(env, 2, "Other")
    other = add_conv(env, 2, "393339990000@s.whatsapp.net")
    configure(env, account_ids=[1])
    inbound(env, other)
    assert jev_runs(env) == []
    inbound(env, conv)
    assert [r["conversation_id"] for r in jev_runs(env)] == [conv]


# --- Classification -------------------------------------------------------------------------------


def test_success_posts_one_noul_per_exit_and_stores_the_chosen_exit(env, conv):
    configure(env)
    add_msg(env, conv, "buongiorno", ts=NOW - 60)
    add_msg(env, conv, "salve, mi dica", direction="out", ts=NOW - 50)
    msg_id = inbound(env, conv)
    env.post_results.append(answer({"person": 0.2, "agent": 0.86, "no_reply": 0.1}))

    assert process(env) == 1

    (call,) = env.posts
    assert call["key"] == KEY and call["timeout"] == 10
    body = call["body"]
    assert set(body) == {"state", "model", "questions"} and body["model"] == "jev-latest"
    assert list(body["questions"]) == ["person", "agent", "no_reply"]
    for exit_ in EXITS:
        question = body["questions"][exit_["id"]]
        assert question["type"] == "noul"
        assert question["instructions"] == {
            "question": env.core.settings.DEFAULT_JEV_QUESTION,
            "condition": exit_["description"],
        }
        assert set(question["criteria"]) == {"true", "false"}
    state = body["state"]
    assert state["latest_message"] == "a che punto è la mia pratica?"
    assert state["recent_messages"] == [
        "contact: buongiorno",
        "us: salve, mi dica",
        "contact: a che punto è la mia pratica?",
    ]
    assert state["number"] == "Main"
    assert state["contact"] == {"name": "Anna", "phone": "+393330001111"}
    assert state["conversation"] == {"state": "new", "tags": [], "handled_by": "agent"}

    result = classification(env, conv)
    assert (result["exit"], result["score"]) == ("agent", 0.86)
    assert result["scores"] == {"person": 0.2, "agent": 0.86, "no_reply": 0.1}
    assert (result["model"], result["message_id"], result["at"]) == ("jev-1.13.0", msg_id, NOW + 5)
    (run,) = jev_runs(env)
    assert (run["status"], run["model"], run["input_tokens"], run["attempts"]) == ("done", "jev-1.13.0", 321, 1)
    assert result["run_id"] == run["id"] and result["latency_ms"] == run["latency_ms"]

    (classified,) = event_rows(env, "conversation.classified")
    assert (classified["conversation_id"], classified["message_id"]) == (conv, msg_id)
    assert json.loads(classified["payload"]) == {"run_id": run["id"], "message_id": msg_id, "exit": "agent", "score": 0.86}
    assert any(json.loads(e["payload"]) == {"fields": ["classification"]} for e in event_rows(env, "conversation.updated"))


def test_no_condition_reaching_its_minimum_chooses_else(env, conv):
    configure(env)
    inbound(env, conv)
    env.post_results.append(answer({"person": 0.69, "agent": 0.5, "no_reply": 0.79}))
    process(env)
    result = classification(env, conv)
    assert (result["exit"], result["score"]) == ("else", None)


def test_the_first_passing_condition_in_list_order_wins(env, conv):
    configure(env)
    inbound(env, conv)
    env.post_results.append(answer({"person": 0.75, "agent": 0.99, "no_reply": 0.1}))
    process(env)
    assert classification(env, conv)["exit"] == "person"


def test_minimum_score_is_inclusive_and_per_condition(env, conv):
    configure(env)
    inbound(env, conv)
    env.post_results.append(answer({"person": 0.1, "agent": 0.1, "no_reply": 0.8}))
    process(env)
    assert classification(env, conv)["exit"] == "no_reply"


def test_a_run_is_not_processed_before_its_debounce_ends(env, conv):
    configure(env)
    inbound(env, conv)
    assert process(env, NOW + 4) == 0 and env.posts == []
    assert process(env, NOW + 5) == 1


def test_summary_text_lists_the_choice_and_every_score(env, conv):
    configure(env)
    cfg = env.core.settings.get_settings(env.conn).jev
    chosen = {"exit": "agent", "score": 0.86, "scores": {"person": 0.2, "agent": 0.86, "no_reply": 0.1}}
    assert env.core.jev.summary_text(cfg, chosen).splitlines() == [
        "Exit condition: Agent can answer (score 0.86)",
        "- Needs a person: 0.20 (min 0.7)",
        "- Agent can answer: 0.86 (min 0.7)",
        "- No reply needed: 0.10 (min 0.8)",
    ]
    nothing = {"exit": "else", "score": None, "scores": {"person": 0.2, "gone": 0.3}}
    assert env.core.jev.summary_text(cfg, nothing).splitlines() == [
        "Exit condition: Else (no condition reached its minimum score)",
        "- Needs a person: 0.20 (min 0.7)",
        "- gone: 0.30",
    ]
    assert env.core.jev.summary_text(cfg, {"exit": "gone", "score": 0.9, "scores": {}}) == "Exit condition: gone (score 0.90)"


@pytest.mark.parametrize(
    ("setup", "reason"),
    [("disabled", "Jev disabled"), ("no_key", "no API key"), ("no_exits", "no exit conditions"), ("gone", "conversation not found")],
)
def test_runs_that_can_no_longer_run_are_skipped(env, conv, setup, reason):
    configure(env)
    inbound(env, conv)
    if setup == "disabled":
        configure(env, enabled=False)
    elif setup == "no_key":
        env.core.jev.set_api_key(env.conn, "", NOW)
    elif setup == "no_exits":
        configure(env, exits=[])
    else:
        env.conn.execute("DELETE FROM conversations WHERE id = ?", (conv,))
    process(env)
    (run,) = jev_runs(env)
    assert (run["status"], run["error"]) == ("skipped", reason)
    assert env.posts == []


# --- Failures and retries -------------------------------------------------------------------------


def test_429_requeues_with_the_retry_after_delay(env, conv):
    configure(env)
    inbound(env, conv)
    env.post_results.append((429, '{"detail":"slow down"}', {"retry-after": "7"}))
    process(env)
    (run,) = jev_runs(env)
    assert (run["status"], run["not_before"], run["attempts"]) == ("queued", NOW + 5 + 7, 1)
    assert run["error"].startswith("Jev HTTP 429")
    assert classification(env, conv) is None


def test_retry_after_is_clamped(env, conv):
    configure(env)
    inbound(env, conv)
    env.post_results.append((429, "", {"retry-after": "99999"}))
    process(env)
    assert jev_runs(env)[0]["not_before"] == NOW + 5 + 300


def test_401_fails_at_once(env, conv):
    configure(env)
    inbound(env, conv)
    env.post_results.append((401, '{"detail":"bad key"}', {}))
    process(env)
    (run,) = jev_runs(env)
    assert run["status"] == "failed" and run["error"] == 'Jev HTTP 401: {"detail":"bad key"}' and run["finished_at"] == NOW + 5
    assert process(env, NOW + 1000) == 0 and len(env.posts) == 1


def test_an_answer_missing_an_exit_fails_the_run(env, conv):
    configure(env)
    inbound(env, conv)
    env.post_results.append(answer({"person": 0.1, "agent": 0.2}))
    process(env)
    (run,) = jev_runs(env)
    assert run["status"] == "failed" and "no_reply" in run["error"]
    assert classification(env, conv) is None and event_rows(env, "conversation.classified") == []


@pytest.mark.parametrize(
    "answers",
    [
        {"person": {"type": "choice", "noul": 0.5}, "agent": {"type": "noul", "noul": 0.5}, "no_reply": {"type": "noul", "noul": 0.5}},
        {"person": {"type": "noul", "noul": 1.5}, "agent": {"type": "noul", "noul": 0.5}, "no_reply": {"type": "noul", "noul": 0.5}},
        {"person": {"type": "noul", "noul": "x"}, "agent": {"type": "noul", "noul": 0.5}, "no_reply": {"type": "noul", "noul": 0.5}},
        {"person": {"type": "noul", "noul": float("nan")}, "agent": {"type": "noul", "noul": 0.5}, "no_reply": {"type": "noul", "noul": 0.5}},
    ],
)
def test_invalid_scores_fail_the_run(env, conv, answers):
    configure(env)
    inbound(env, conv)
    env.post_results.append((200, json.dumps({"answers": answers}), {}))
    process(env)
    assert jev_runs(env)[0]["status"] == "failed"


def test_a_non_json_answer_fails_the_run(env, conv):
    configure(env)
    inbound(env, conv)
    env.post_results.append((200, "<html>", {}))
    process(env)
    assert jev_runs(env)[0]["status"] == "failed"


def test_unreachable_backs_off_5_then_30_seconds_then_fails(env, conv):
    configure(env)
    inbound(env, conv)
    env.post_results.extend(RuntimeError("Jev unreachable: timed out") for _ in range(3))
    process(env, NOW + 5)
    assert (jev_runs(env)[0]["status"], jev_runs(env)[0]["not_before"]) == ("queued", NOW + 5 + 5)
    assert process(env, NOW + 9) == 0
    process(env, NOW + 10)
    assert (jev_runs(env)[0]["status"], jev_runs(env)[0]["not_before"]) == ("queued", NOW + 10 + 30)
    process(env, NOW + 40)
    (run,) = jev_runs(env)
    assert (run["status"], run["attempts"], run["error"]) == ("failed", 3, "Jev unreachable: timed out")
    assert len(env.posts) == 3


def test_a_5xx_is_retried_and_a_run_interrupted_while_running_comes_back(env, conv):
    configure(env)
    inbound(env, conv)
    env.post_results.append((529, "overloaded", {}))
    process(env)
    assert jev_runs(env)[0]["status"] == "queued"

    env.conn.execute("UPDATE jev_runs SET status = 'running', started_at = ?, not_before = NULL", (NOW,))
    assert process(env, NOW + 30) == 0  # still inside timeout_s + 60
    assert jev_runs(env)[0]["status"] == "running"
    process(env, NOW + 100)  # stale: requeued, then claimed and run
    assert jev_runs(env)[0]["status"] == "done"


# --- Automations -------------------------------------------------------------------------------------


def test_a_person_rule_takes_the_conversation_over_when_that_exit_is_chosen(env, conv):
    configure(env)
    rule = mk_rule(
        env, {"type": "takeover"}, event_types=["conversation.classified"], conditions={"jev_exits": ["person"]}, reply_mode="none"
    )
    inbound(env, conv)
    env.post_results.append(answer({"person": 0.9, "agent": 0.2, "no_reply": 0.1}))
    process(env)
    (run,) = automation_runs(env)
    assert (run["rule_id"], run["status"]) == (rule["id"], "queued")
    assert env.core.automations.process_due(env.core.db.connect, NOW + 6) == 1
    assert env.conn.execute("SELECT agent_active, state FROM conversations WHERE id = ?", (conv,)).fetchone()[:] == (0, "in_progress")
    assert automation_runs(env)[0]["status"] == "done"


def test_a_rule_for_another_exit_does_not_fire(env, conv):
    configure(env)
    mk_rule(env, {"type": "takeover"}, event_types=["conversation.classified"], conditions={"jev_exits": ["person", "agent"]}, reply_mode="none")
    inbound(env, conv)
    env.post_results.append(answer({"person": 0.1, "agent": 0.2, "no_reply": 0.9}))
    process(env)
    assert automation_runs(env) == []


def test_the_else_rule_runs_when_nothing_passes(env, conv):
    configure(env)
    mk_rule(env, {"type": "escalate"}, event_types=["conversation.classified"], conditions={"jev_exits": ["else"]}, reply_mode="none")
    inbound(env, conv)
    env.post_results.append(answer({"person": 0.1, "agent": 0.2, "no_reply": 0.3}))
    process(env)
    assert len(automation_runs(env)) == 1
    env.core.automations.process_due(env.core.db.connect, NOW + 6)
    assert env.conn.execute("SELECT priority FROM conversations WHERE id = ?", (conv,)).fetchone()[0] == 2


def test_dry_run_without_a_classification_says_so(env, conv):
    configure(env)
    rule = mk_rule(env, {"type": "takeover"}, event_types=["conversation.classified"], conditions={"jev_exits": ["person"]}, reply_mode="none")
    add_msg(env, conv)
    body = env.client.post(f"{PREFIX}/automations/{rule['id']}/test", json={"conversation_id": conv}).json()
    assert body["matches"] is False
    assert "no: no Jev classification yet" in body["reasons"]

    env.conn.execute(
        "UPDATE conversations SET classification = ? WHERE id = ?",
        (json.dumps({"exit": "agent", "score": 0.9, "scores": {"agent": 0.9}}), conv),
    )
    body = env.client.post(f"{PREFIX}/automations/{rule['id']}/test", json={"conversation_id": conv}).json()
    assert body["matches"] is False
    assert any("Jev exit condition is agent; rule wants ['person']" in r for r in body["reasons"])


def test_the_jev_template_variable_renders_the_summary_in_a_hermes_prompt(env, conv):
    configure(env)
    mk_rule(
        env,
        {"type": "hermes", "profile": None, "prompt": "Answer {contact_name}.\n\nJev:\n{jev}"},
        event_types=["conversation.classified"],
        conditions={"jev_exits": ["agent"]},
        reply_mode="draft",
    )
    inbound(env, conv)
    env.post_results.append(answer({"person": 0.2, "agent": 0.86, "no_reply": 0.1}))
    process(env)
    env.core.automations.process_due(env.core.db.connect, NOW + 6)
    (command,) = env.commands
    prompt = command["argv"][command["argv"].index("-q") + 1]
    assert "Exit condition: Agent can answer (score 0.86)" in prompt
    assert "- Needs a person: 0.20 (min 0.7)" in prompt
    assert env.conn.execute("SELECT COUNT(*) FROM messages WHERE status = 'draft'").fetchone()[0] == 1


def test_the_jev_variable_is_empty_without_a_classification(env, conv):
    configure(env)
    conv_row = env.core.automations._conversation(env.conn, conv)
    assert env.core.automations._template_vars(env.conn, env.core.automations._cfg(env.conn), conv_row, "x")["jev"] == ""


def test_webhook_payload_carries_the_classification(env, conv):
    configure(env)
    inbound(env, conv)
    env.post_results.append(answer({"person": 0.2, "agent": 0.86, "no_reply": 0.1}))
    process(env)
    payload = json.loads(env.core.automations._payload(env.conn, env.core.automations._cfg(env.conn), {"id": 1, "rule_id": 1}, {"id": 1, "type": "conversation.classified", "message_id": None, "at": NOW, "payload": "{}"}, env.core.automations._conversation(env.conn, conv)))
    assert payload["conversation"]["classification"]["exit"] == "agent"


def test_conditions_validate_jev_exit_ids(env, conv):
    body = {"name": "r", "action": {"type": "takeover"}, "event_types": ["conversation.classified"]}
    ok = env.client.post(f"{PREFIX}/automations", json={**body, "conditions": {"jev_exits": ["person", "else"]}})
    assert ok.status_code == 200 and ok.json()["conditions"] == {"jev_exits": ["person", "else"]}
    assert env.client.post(f"{PREFIX}/automations", json={**body, "conditions": {"jev_exits": ["Bad Id"]}}).status_code == 422


def test_a_broken_jev_hook_never_rolls_back_the_message(env, conv, monkeypatch):
    configure(env)

    def boom(*a, **k):
        raise RuntimeError("boom")

    monkeypatch.setattr(env.core.jev, "enqueue_for_message", boom)
    msg_id = inbound(env, conv)
    assert env.conn.execute("SELECT COUNT(*) FROM messages WHERE id = ?", (msg_id,)).fetchone()[0] == 1
    assert event_rows(env, "message.in")


# --- Routes ---------------------------------------------------------------------------------------------


def test_test_route_scores_the_latest_message_and_persists_nothing(env, conv):
    configure(env, enabled=False)  # a test works before Jev is switched on
    add_msg(env, conv)
    env.post_results.append(answer({"person": 0.2, "agent": 0.86, "no_reply": 0.1}))
    before = env.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]

    r = env.client.post(f"{PREFIX}/jev/test", json={"conversation_id": conv})

    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"state", "scores", "exit", "score", "summary", "model", "latency_ms", "usage"}
    assert (body["exit"], body["score"], body["model"]) == ("agent", 0.86, "jev-1.13.0")
    assert body["scores"] == {"person": 0.2, "agent": 0.86, "no_reply": 0.1}
    assert body["summary"].startswith("Exit condition: Agent can answer (score 0.86)")
    assert body["usage"] == {"input_tokens": 321, "output_tokens": 0}
    assert body["state"]["latest_message"] == "a che punto è la mia pratica?"
    assert jev_runs(env) == [] and classification(env, conv) is None
    assert env.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == before


def test_test_route_errors(env, conv):
    add_msg(env, conv)
    url = f"{PREFIX}/jev/test"
    assert env.client.post(url, json={"conversation_id": conv}).status_code == 400  # no key, no exits
    configure(env, key=None)
    assert env.client.post(url, json={"conversation_id": conv}).status_code == 400  # no key
    configure(env)
    assert env.client.post(url, json={"conversation_id": 999}).status_code == 404
    env.post_results.append((401, '{"detail":"bad key"}', {}))
    r = env.client.post(url, json={"conversation_id": conv})
    assert r.status_code == 502 and "401" in r.json()["detail"] and "bad key" in r.json()["detail"]
    env.post_results.append(RuntimeError("Jev unreachable: timed out"))
    r = env.client.post(url, json={"conversation_id": conv})
    assert r.status_code == 503 and "unreachable" in r.json()["detail"]
    env.post_results.append((200, json.dumps({"answers": {}}), {}))
    assert env.client.post(url, json={"conversation_id": conv}).status_code == 502


def test_classify_now_queues_a_run_due_immediately_and_coalesces(env, conv):
    configure(env)
    first = inbound(env, conv)
    before = int(time.time())
    r = env.client.post(f"{PREFIX}/conversations/{conv}/classify")
    assert r.status_code == 200 and r.json()["queued"] is True
    (run,) = jev_runs(env)
    assert run["id"] == r.json()["run_id"] and run["status"] == "queued"
    assert run["message_id"] == first
    assert before <= run["not_before"] <= int(time.time())
    assert env.client.post(f"{PREFIX}/conversations/{conv}/classify").json()["run_id"] == run["id"]
    assert len(jev_runs(env)) == 1


def test_classify_errors(env, conv):
    url = f"{PREFIX}/conversations/{conv}/classify"
    assert env.client.post(url).status_code == 400  # Jev disabled
    configure(env, enabled=False)
    assert env.client.post(url).status_code == 400
    configure(env)
    assert env.client.post(url).status_code == 400  # nothing incoming to classify
    assert env.client.post(f"{PREFIX}/conversations/999/classify").status_code == 404
    configure(env, key=None)
    env.core.jev.set_api_key(env.conn, "", NOW)
    add_msg(env, conv)
    assert env.client.post(url).status_code == 400  # no key
    configure(env, exits=[])
    assert env.client.post(url).status_code == 400  # no exits


def test_conversation_detail_carries_the_classification_and_the_last_five_runs(env, conv):
    configure(env)
    detail = env.client.get(f"{PREFIX}/conversations/{conv}").json()
    assert detail["classification"] is None and detail["conversation"]["classification"] is None
    assert detail["jev_runs"] == []

    for i in range(6):
        inbound(env, conv, f"msg {i}", now=NOW + i * 100)
        process(env, NOW + i * 100 + 5)
    detail = env.client.get(f"{PREFIX}/conversations/{conv}").json()
    assert detail["classification"]["exit"] == "agent"
    assert detail["conversation"]["classification"] == detail["classification"]
    assert len(detail["jev_runs"]) == 5
    assert [r["id"] for r in detail["jev_runs"]] == sorted((r["id"] for r in detail["jev_runs"]), reverse=True)
    assert detail["jev_runs"][0]["status"] == "done"


# --- Deleting and merging conversations -----------------------------------------------------------------------


def _run_for(env, conv_id: int) -> None:
    env.conn.execute(
        "INSERT INTO jev_runs (conversation_id, account_id, status, created_at) VALUES (?, 1, 'done', ?)", (conv_id, NOW)
    )


def test_purge_conversations_removes_jev_runs(env, conv):
    add_account(env, 2, "Other")
    other = add_conv(env, 2, "393339990000@s.whatsapp.net")
    _run_for(env, conv)
    _run_for(env, other)
    with env.core.db.write_txn(env.conn):
        env.core.accounts.purge_conversations(env.conn, 1)
    assert [r["conversation_id"] for r in jev_runs(env)] == [other]


def test_delete_conversation_removes_jev_runs(env, conv):
    other = add_conv(env, 1, "393339990000@s.whatsapp.net")
    _run_for(env, conv)
    _run_for(env, other)
    with env.core.db.write_txn(env.conn):
        env.core.contacts.delete_conversation(env.conn, conv)
    assert [r["conversation_id"] for r in jev_runs(env)] == [other]


def test_merge_moves_jev_runs_to_the_surviving_conversation(env, conv):
    other = add_conv(env, 1, "393339990000@s.whatsapp.net")
    _run_for(env, conv)
    src = env.conn.execute("SELECT * FROM conversations WHERE id = ?", (conv,)).fetchone()
    dst = env.conn.execute("SELECT * FROM conversations WHERE id = ?", (other,)).fetchone()
    with env.core.db.write_txn(env.conn):
        env.core.contacts._merge(env.conn, src, dst, NOW)
    assert [r["conversation_id"] for r in jev_runs(env)] == [other]
    assert env.conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conv,)).fetchone() is None


def test_post_sends_the_documented_http_request(env, monkeypatch):
    """The real `_post` builds the Bearer request and maps transport failures (urllib faked)."""
    import io
    import urllib.error
    from email.message import Message

    seen = {}

    class Resp:
        status = 200
        headers = {"Retry-After": "3"}

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self, n=-1):
            return b'{"answers": {}}'

    def fake_open(request, timeout):
        seen.update(url=request.full_url, method=request.get_method(), headers=dict(request.header_items()), data=request.data, timeout=timeout)
        return Resp()

    monkeypatch.setattr("urllib.request.urlopen", fake_open)
    status, text, headers = env.real_post(b'{"x":1}', "KEY", 7)
    assert (status, text, headers) == (200, '{"answers": {}}', {"retry-after": "3"})
    assert seen["url"] == "https://api.typesafe.ai/v1/systemone" and seen["method"] == "POST" and seen["timeout"] == 7
    assert seen["headers"]["Authorization"] == "Bearer KEY" and seen["headers"]["Content-type"] == "application/json"
    assert seen["headers"]["User-agent"] == "hermes-whatsapp-chat" and seen["data"] == b'{"x":1}'

    def http_error(request, timeout):
        headers = Message()
        headers["Retry-After"] = "9"
        raise urllib.error.HTTPError("u", 429, "Too Many", headers, io.BytesIO(b"slow"))

    monkeypatch.setattr("urllib.request.urlopen", http_error)
    assert env.real_post(b"{}", "KEY", 1) == (429, "slow", {"retry-after": "9"})

    def down(request, timeout):
        raise urllib.error.URLError("no route")

    monkeypatch.setattr("urllib.request.urlopen", down)
    with pytest.raises(RuntimeError, match="Jev unreachable: no route"):
        env.real_post(b"{}", "KEY", 1)

    class Huge(Resp):
        def read(self, n=-1):
            return b"x" * (env.core.jev.MAX_RESPONSE_BYTES + 1)

    monkeypatch.setattr("urllib.request.urlopen", lambda request, timeout: Huge())
    with pytest.raises(RuntimeError, match="too large"):
        env.real_post(b"{}", "KEY", 1)
