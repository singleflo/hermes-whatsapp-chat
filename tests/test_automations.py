"""Automation rules, run queue and executor for hermes-whatsapp-chat.

Loads the REAL plugin/dashboard/plugin_api.py (which loads wa_core) against a throwaway
SQLite DB. Faked boundaries only: subprocess (``automations._run_command``), webhook HTTP
(``automations._http_post``) and the WhatsApp bridge (``core.bridge.bridge_request``).
"""

from __future__ import annotations

import hashlib
import hmac
import importlib.util
import json
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = REPO_ROOT / "plugin" / "dashboard" / "plugin_api.py"
PREFIX = "/api/plugins/hermes-whatsapp-chat"
CONTACT_JID = "393330001111@s.whatsapp.net"
# Wednesday 2026-01-14 12:00 in Europe/Rome: inside default business hours.
NOW = int(datetime(2026, 1, 14, 12, 0, tzinfo=ZoneInfo("Europe/Rome")).timestamp())
SATURDAY = int(datetime(2026, 1, 17, 12, 0, tzinfo=ZoneInfo("Europe/Rome")).timestamp())


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- Fixtures ------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WA_ARCHIVE_DB", str(tmp_path / "wa_board.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    plugin = _load("hermes_dashboard_plugin_hermes_whatsapp_chat_automations", PLUGIN_FILE)
    core = plugin.core
    app = FastAPI()
    app.include_router(plugin.router, prefix=PREFIX)

    commands: list[dict] = []
    posts: list[dict] = []
    bridge_calls: list[tuple] = []
    command_results: list = []
    post_results: list = []

    def fake_run(argv, *, stdin=None, timeout, env=None):
        commands.append({"argv": list(argv), "stdin": stdin, "timeout": timeout})
        result = command_results.pop(0) if command_results else (0, "default reply\n", "")
        if isinstance(result, Exception):
            raise result
        return result

    def fake_post(url, body, headers, timeout):
        posts.append({"url": url, "body": body, "headers": headers, "timeout": timeout})
        result = post_results.pop(0) if post_results else (200, "")
        if isinstance(result, Exception):
            raise result
        return result

    def fake_bridge(port, method, path, payload=None, *, timeout):
        bridge_calls.append((port, method, path, payload))
        return 200, {"success": True, "messageId": f"WAID{len(bridge_calls)}"}

    monkeypatch.setattr(core.automations, "_run_command", fake_run)
    monkeypatch.setattr(core.automations, "_http_post", fake_post)
    monkeypatch.setattr(core.bridge, "bridge_request", fake_bridge)

    conn = core.db.connect()
    yield SimpleNamespace(
        core=core,
        client=TestClient(app),
        conn=conn,
        commands=commands,
        posts=posts,
        bridge_calls=bridge_calls,
        command_results=command_results,
        post_results=post_results,
    )
    conn.close()


# --- Seed helpers (SQL on the contract schema) -----------------------------------------


def add_account(env, account_id: int = 1, label: str = "Main", kind: str = "whatsapp", profile: str | None = None) -> int:
    env.conn.execute(
        "INSERT INTO accounts (id, kind, label, port, session_dir, desired, hermes_profile, created_at)"
        " VALUES (?,?,?,?,?,'running',?,?)",
        (account_id, kind, label, 3016 + account_id, f"sessions/{account_id}", profile, NOW),
    )
    return account_id


def pair_account(env, account_id: int = 1) -> None:
    account = env.core.accounts.get_account(env.conn, account_id)
    session = env.core.accounts.session_path(account)
    session.mkdir(parents=True, exist_ok=True)
    (session / "creds.json").write_text("{}", encoding="utf-8")


def add_conv(
    env,
    account_id: int = 1,
    jid: str = CONTACT_JID,
    state: str = "new",
    agent_active: int = 1,
    tags: list[str] | None = None,
    name: str = "Anna",
) -> int:
    cur = env.conn.execute(
        "INSERT INTO conversations (account_id, chat_jid, contact_name, phone, state, agent_active, tags,"
        " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (account_id, jid, name, jid.split("@")[0], state, agent_active, json.dumps(tags or []), NOW, NOW),
    )
    return cur.lastrowid


def add_msg(
    env,
    conv_id: int,
    body: str = "Hello, I would like the PRICE list",
    direction: str = "in",
    author: str | None = None,
    source: str = "live",
    ts: int = NOW,
    status: str | None = None,
) -> int:
    account_id = env.conn.execute("SELECT account_id FROM conversations WHERE id = ?", (conv_id,)).fetchone()[0]
    author = author or ("contact" if direction == "in" else "user")
    status = status or ("received" if direction == "in" else "sent")
    cur = env.conn.execute(
        "INSERT INTO messages (conversation_id, account_id, direction, author, body, ts, status, source)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (conv_id, account_id, direction, author, body, ts, status, source),
    )
    return cur.lastrowid


def fire(env, event_type: str, conv_id: int, message_id: int | None = None, payload: dict | None = None, now: int = NOW) -> int:
    account_id = env.conn.execute("SELECT account_id FROM conversations WHERE id = ?", (conv_id,)).fetchone()[0]
    with env.core.db.write_txn(env.conn):
        return env.core.events.emit(
            env.conn, event_type, now=now, account_id=account_id, conversation_id=conv_id, message_id=message_id, payload=payload
        )


def inbound(env, conv_id: int, body: str = "Hello, I would like the PRICE list", **kw) -> int:
    msg_id = add_msg(env, conv_id, body, **kw)
    fire(env, "message.in", conv_id, msg_id)
    return msg_id


def mk_rule(env, action: dict | None = None, **fields) -> dict:
    body = {
        "name": "rule",
        "action": action or {"type": "hermes", "profile": None, "prompt": "Reply to {text}"},
        **fields,
    }
    res = env.client.post(f"{PREFIX}/automations", json=body)
    assert res.status_code == 200, res.text
    return res.json()


def runs(env) -> list[dict]:
    return [dict(r) for r in env.conn.execute("SELECT * FROM automation_runs ORDER BY id")]


def process(env, now: int = NOW, limit: int = 4) -> int:
    return env.core.automations.process_due(env.core.db.connect, now, limit=limit)


def set_automation_settings(env, **fields) -> None:
    cfg = env.core.settings.get_settings(env.conn).model_dump()
    cfg["automations"].update(fields)
    env.conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('global', ?, ?)", (json.dumps(cfg), NOW)
    )


def one(env, sql: str, args: tuple = ()):
    return env.conn.execute(sql, args).fetchone()


@pytest.fixture
def conv(env):
    add_account(env)
    return add_conv(env, tags=["vip"])


# --- Matching: conditions ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("conditions", "expected"),
    [
        ({}, True),
        ({"states": ["new", "waiting"]}, True),
        ({"states": ["closed"]}, False),
        ({"agent_active": True}, True),
        ({"agent_active": False}, False),
        ({"business_hours": "in"}, True),
        ({"business_hours": "out"}, False),
        ({"text_contains": ["price"]}, True),
        ({"text_contains": ["discount", "PRICE"]}, True),
        ({"text_contains": ["discount"]}, False),
        ({"text_regex": r"like\s+the"}, True),
        ({"text_regex": r"^price"}, False),
        ({"tags_any": ["VIP"]}, True),
        ({"tags_any": ["lead", "churn"]}, False),
        ({"first_message": True}, True),
        ({"first_message": False}, False),
        ({"directions": ["in"]}, True),
        ({"directions": ["out"]}, False),
        ({"states": ["new"], "text_contains": ["discount"]}, False),
        ({"states": ["new"], "tags_any": ["vip"], "first_message": True, "business_hours": "in"}, True),
    ],
)
def test_condition_matching(env, conv, conditions, expected):
    mk_rule(env, conditions=conditions)
    inbound(env, conv)
    assert [r["status"] for r in runs(env)] == (["queued"] if expected else [])


def test_invalid_regex_never_matches(env, conv):
    rule = mk_rule(env)
    env.conn.execute("UPDATE automation_rules SET conditions = ? WHERE id = ?", (json.dumps({"text_regex": "("}), rule["id"]))
    inbound(env, conv)
    assert runs(env) == []


def test_business_hours_out_on_saturday(env, conv):
    mk_rule(env, conditions={"business_hours": "out"})
    msg_id = add_msg(env, conv, ts=SATURDAY)
    fire(env, "message.in", conv, msg_id, now=SATURDAY)
    assert len(runs(env)) == 1


def test_first_message_is_false_for_a_second_inbound(env, conv):
    mk_rule(env, conditions={"first_message": True})
    add_msg(env, conv, "earlier", ts=NOW - 60)
    inbound(env, conv)
    assert runs(env) == []


def test_event_types_filter(env, conv):
    mk_rule(env, event_types=["message.out"])
    inbound(env, conv)
    assert runs(env) == []
    out_id = add_msg(env, conv, "we answered", direction="out")
    fire(env, "message.out", conv, out_id)
    assert len(runs(env)) == 1


def test_conversation_created_and_state_changed_trigger(env, conv):
    mk_rule(env, event_types=["conversation.created"], conditions={"first_message": True})
    mk_rule(env, name="state", event_types=["conversation.state_changed"], conditions={"states": ["closed"]})
    fire(env, "conversation.created", conv)
    assert len(runs(env)) == 1
    env.conn.execute("UPDATE conversations SET state = 'closed' WHERE id = ?", (conv,))
    fire(env, "conversation.state_changed", conv, payload={"from": "new", "to": "closed", "actor": "user", "reason": None})
    assert len(runs(env)) == 2


def test_state_change_by_rule_does_not_retrigger(env, conv):
    mk_rule(env, event_types=["conversation.state_changed"])
    fire(env, "conversation.state_changed", conv, payload={"from": "new", "to": "closed", "actor": "rule:9", "reason": None})
    assert runs(env) == []


def test_disabled_rule_and_disabled_setting(env, conv):
    mk_rule(env, enabled=False)
    inbound(env, conv)
    assert runs(env) == []
    mk_rule(env, name="on")
    set_automation_settings(env, enabled=False)
    inbound(env, conv)
    assert runs(env) == []


def test_non_triggerable_event_creates_no_runs(env, conv):
    mk_rule(env)
    msg_id = add_msg(env, conv, "draft text", direction="out", author="user", status="draft")
    fire(env, "message.draft", conv, msg_id)
    assert runs(env) == []
    assert one(env, "SELECT COUNT(*) FROM events WHERE type = 'message.draft'")[0] == 1


def test_emit_stores_payload_and_returns_id(env, conv):
    event_id = fire(env, "conversation.updated", conv, payload={"fields": ["tags"]})
    row = one(env, "SELECT * FROM events WHERE id = ?", (event_id,))
    assert row["type"] == "conversation.updated"
    assert json.loads(row["payload"]) == {"fields": ["tags"]}
    assert row["at"] == NOW and row["account_id"] == 1


# --- Matching: scoping, ordering, guards ---------------------------------------------------


def test_account_scoping(env, conv):
    add_account(env, 2, "Second")
    other = add_conv(env, account_id=2, jid="393330002222@s.whatsapp.net")
    scoped = mk_rule(env, name="only account 2", account_id=2)
    everyone = mk_rule(env, name="all accounts", account_id=None)
    inbound(env, conv)
    assert [r["rule_id"] for r in runs(env)] == [everyone["id"]]
    inbound(env, other)
    assert sorted(r["rule_id"] for r in runs(env)) == sorted([everyone["id"], everyone["id"], scoped["id"]])


def test_stop_after_match_halts_later_rules(env, conv):
    first = mk_rule(env, name="first", stop_after_match=True)
    mk_rule(env, name="second")
    inbound(env, conv)
    assert [r["rule_id"] for r in runs(env)] == [first["id"]]


def test_rules_run_in_position_order(env, conv):
    a = mk_rule(env, name="a")
    b = mk_rule(env, name="b")
    res = env.client.post(f"{PREFIX}/automations/reorder", json={"ids": [b["id"], a["id"]]})
    assert [r["id"] for r in res.json()["rules"]] == [b["id"], a["id"]]
    inbound(env, conv)
    assert [r["rule_id"] for r in runs(env)] == [b["id"], a["id"]]


def test_stop_after_match_only_counts_matching_rules(env, conv):
    mk_rule(env, name="no match", conditions={"states": ["closed"]}, stop_after_match=True)
    second = mk_rule(env, name="second")
    inbound(env, conv)
    assert [r["rule_id"] for r in runs(env)] == [second["id"]]


@pytest.mark.parametrize("author", ["agent:coder", "agent:default", "rule:7"])
def test_loop_guard_automation_authored_messages_never_trigger(env, conv, author):
    mk_rule(env, event_types=["message.in", "message.out"])
    msg_id = add_msg(env, conv, "automated", direction="out", author=author)
    fire(env, "message.out", conv, msg_id)
    assert runs(env) == []


def test_history_messages_never_trigger(env, conv):
    mk_rule(env)
    inbound(env, conv, source="history")
    assert runs(env) == []


def test_rate_limit_inserts_skipped_run(env, conv):
    mk_rule(env)
    set_automation_settings(env, max_runs_per_conversation_per_hour=2)
    for _ in range(3):
        inbound(env, conv)
    statuses = [(r["status"], r["error"]) for r in runs(env)]
    assert statuses == [("queued", None), ("queued", None), ("skipped", "rate limited")]
    # the window slides: an hour later the conversation may trigger again
    msg_id = add_msg(env, conv, "later", ts=NOW + 3700)
    fire(env, "message.in", conv, msg_id, now=NOW + 3700)
    assert runs(env)[-1]["status"] == "queued"


# --- Executing: hermes ----------------------------------------------------------------------


def test_hermes_action_creates_draft_with_agent_author(env, conv):
    add_msg(env, conv, "Earlier question", ts=NOW - 60)
    rule = mk_rule(
        env,
        action={
            "type": "hermes",
            "profile": "coder",
            "prompt": "Customer {contact_name} ({phone}) on {account_label} is {state}: {text}\n{history}\nCLI={wa_cli} {nope}",
            "skills": ["whatsapp-chat"],
        },
    )
    env.command_results.append((0, "Hi Anna, here is the list.\nsession_id: 2026_abc\n", ""))
    inbound(env, conv)
    assert process(env) == 1

    argv = env.commands[0]["argv"]
    assert Path(argv[0]).name == "hermes"
    prompt = argv[argv.index("-q") + 1]
    assert argv[1:5] == ["-p", "coder", "chat", "-Q"]
    assert argv[argv.index("--source") + 1] == "whatsapp-chat"
    assert argv[-2:] == ["-s", "whatsapp-chat"]
    assert env.commands[0]["timeout"] == 180
    assert prompt.startswith("Customer Anna (393330001111) on Main is new: Hello, I would like the PRICE list")
    assert "] user:" not in prompt and "contact: Earlier question" in prompt and "contact: Hello, I would like" in prompt
    wa = env.core.db.data_dir() / "bin" / "wa"
    assert f"CLI={wa} " in prompt and wa.is_file() and "{" not in prompt and prompt.endswith(" ")

    draft = one(env, "SELECT * FROM messages WHERE status = 'draft'")
    assert draft["body"] == "Hi Anna, here is the list."
    assert draft["author"] == "agent:coder" and draft["rule_id"] == rule["id"] and draft["direction"] == "out"
    assert env.bridge_calls == []
    run = runs(env)[0]
    assert run["status"] == "done" and run["attempts"] == 1 and run["finished_at"] == NOW
    assert "Hi Anna, here is the list." in run["output"]
    ev = one(env, "SELECT * FROM events WHERE type = 'automation.run'")
    assert json.loads(ev["payload"]) == {"rule_id": rule["id"], "run_id": run["id"], "status": "done"}


def test_hermes_uses_account_profile_when_rule_has_none(env):
    add_account(env, profile="support")
    conv_id = add_conv(env)
    mk_rule(env)
    inbound(env, conv_id)
    process(env)
    assert env.commands[0]["argv"][1:3] == ["-p", "support"]
    assert one(env, "SELECT author FROM messages WHERE status = 'draft'")["author"] == "agent:support"


def test_hermes_send_mode_sends_through_bridge(env, conv):
    pair_account(env)
    rule = mk_rule(env, reply_mode="send")
    env.command_results.append((0, "On it!\n", ""))
    inbound(env, conv)
    process(env)

    assert env.bridge_calls == [(3017, "POST", "/send", {"chatId": CONTACT_JID, "message": "On it!"})]
    sent = one(env, "SELECT * FROM messages WHERE direction = 'out'")
    assert sent["status"] == "sent" and sent["author"] == "agent:default" and sent["rule_id"] == rule["id"]
    assert runs(env)[0]["status"] == "done"
    # the outbound message.out carries an agent author: it must not retrigger anything
    assert len(runs(env)) == 1


def test_reply_mode_none_only_stores_output(env, conv):
    mk_rule(env, reply_mode="none")
    env.command_results.append((0, "secret thoughts", ""))
    inbound(env, conv)
    process(env)
    assert one(env, "SELECT COUNT(*) FROM messages WHERE direction = 'out'")[0] == 0
    assert env.bridge_calls == []
    run = runs(env)[0]
    assert run["status"] == "done" and "secret thoughts" in run["output"]


def test_agent_inactive_skips_reply_actions(env):
    add_account(env)
    conv_id = add_conv(env, agent_active=0)
    mk_rule(env)
    inbound(env, conv_id)
    process(env)
    run = runs(env)[0]
    assert run["status"] == "skipped" and "agent_active" in run["error"]
    assert env.commands == []
    assert one(env, "SELECT COUNT(*) FROM messages WHERE status = 'draft'")[0] == 0


def test_agent_inactive_still_runs_non_reply_actions(env):
    add_account(env)
    conv_id = add_conv(env, agent_active=0)
    mk_rule(env, action={"type": "add_tags", "tags": ["seen"]})
    inbound(env, conv_id)
    process(env)
    assert runs(env)[0]["status"] == "done"
    assert json.loads(one(env, "SELECT tags FROM conversations WHERE id = ?", (conv_id,))["tags"]) == ["seen"]


def test_failure_retries_with_backoff_then_fails(env, conv):
    mk_rule(env)
    env.command_results.extend([(1, "", "boom")] * 3)
    inbound(env, conv)

    assert process(env, NOW) == 1
    run = runs(env)[0]
    assert (run["status"], run["attempts"], run["not_before"]) == ("queued", 1, NOW + 30)
    assert "boom" in run["error"]

    assert process(env, NOW + 29) == 0  # backoff not elapsed
    assert process(env, NOW + 30) == 1
    run = runs(env)[0]
    assert (run["status"], run["attempts"], run["not_before"]) == ("queued", 2, NOW + 30 + 120)

    assert process(env, NOW + 150) == 1
    run = runs(env)[0]
    assert (run["status"], run["attempts"]) == ("failed", 3)
    assert "boom" in run["error"] and run["finished_at"] == NOW + 150
    assert len(env.commands) == 3
    statuses = [json.loads(r["payload"])["status"] for r in env.conn.execute("SELECT payload FROM events WHERE type = 'automation.run'")]
    assert statuses == ["failed"]  # retries do not emit, the final outcome does


def test_empty_reply_and_timeout_are_failures(env, conv):
    mk_rule(env)
    env.command_results.extend([(0, "session_id: abc\n", ""), RuntimeError("command timed out after 180s")])
    inbound(env, conv)
    process(env, NOW)
    assert "empty reply" in runs(env)[0]["error"]
    process(env, NOW + 30)
    assert "timed out" in runs(env)[0]["error"]


def test_process_due_respects_limit_and_claims_once(env, conv):
    mk_rule(env)
    for _ in range(3):
        inbound(env, conv)
    assert process(env, limit=2) == 2
    assert [r["status"] for r in runs(env)] == ["done", "done", "queued"]
    assert process(env) == 1
    assert process(env) == 0


def test_stale_running_runs_are_recovered(env, conv):
    rule = mk_rule(env, action={"type": "hermes", "profile": None, "prompt": "p", "timeout_s": 100})
    event_id = fire(env, "conversation.created", conv)
    insert = (
        "INSERT INTO automation_runs (rule_id, event_id, conversation_id, status, attempts, created_at, started_at)"
        " VALUES (?,?,?,'running',?,?,?)"
    )
    fresh = env.conn.execute(insert, (rule["id"], event_id, conv, 1, NOW, NOW - 100)).lastrowid  # < 160 s: still running
    stale = env.conn.execute(insert, (rule["id"], event_id, conv, 1, NOW, NOW - 200)).lastrowid  # > 160 s: back to queue
    spent = env.conn.execute(insert, (rule["id"], event_id, conv, 3, NOW, NOW - 200)).lastrowid  # no attempts left
    assert process(env) == 1
    by_id = {r["id"]: r for r in runs(env)}
    assert by_id[fresh]["status"] == "running"
    assert by_id[stale]["status"] == "done" and by_id[stale]["attempts"] == 2
    assert by_id[spent]["status"] == "failed" and "interrupted" in by_id[spent]["error"]


def test_run_for_deleted_rule_is_skipped(env, conv):
    rule = mk_rule(env)
    inbound(env, conv)
    assert env.client.delete(f"{PREFIX}/automations/{rule['id']}").json() == {"ok": True}
    process(env)
    run = runs(env)[0]
    assert run["status"] == "skipped" and run["error"] == "rule deleted"
    assert env.commands == []


# --- Executing: webhook, script, simple actions -------------------------------------------------


def test_webhook_signature_and_directives(env, conv):
    rule = mk_rule(
        env,
        action={"type": "webhook", "url": "https://hook.example/wa", "secret": "s3cret"},
        reply_mode="draft",
    )
    env.post_results.append(
        (200, json.dumps({"reply": "Thanks, we will call you", "state": "in_progress", "tags": ["lead"], "escalate": True}))
    )
    msg_id = inbound(env, conv)
    process(env)

    post = env.posts[0]
    assert post["url"] == "https://hook.example/wa"
    expected = "sha256=" + hmac.new(b"s3cret", post["body"], hashlib.sha256).hexdigest()
    assert post["headers"]["X-WA-Signature"] == expected
    body = json.loads(post["body"])
    assert set(body) == {"event", "conversation", "messages"}
    assert body["event"]["type"] == "message.in" and body["event"]["message_id"] == msg_id
    assert body["conversation"]["id"] == conv and body["conversation"]["chat_jid"] == CONTACT_JID
    assert body["messages"][-1]["body"] == "Hello, I would like the PRICE list"

    assert runs(env)[0]["status"] == "done"
    draft = one(env, "SELECT * FROM messages WHERE status = 'draft'")
    assert draft["body"] == "Thanks, we will call you" and draft["author"] == f"rule:{rule['id']}"
    row = one(env, "SELECT * FROM conversations WHERE id = ?", (conv,))
    assert row["state"] == "in_progress" and row["previous_state"] == "new" and row["priority"] == 2
    assert json.loads(row["tags"]) == ["vip", "lead"]
    log = one(env, "SELECT * FROM conversation_state_log WHERE conversation_id = ?", (conv,))
    assert (log["from_state"], log["to_state"], log["actor"]) == ("new", "in_progress", f"rule:{rule['id']}")
    assert one(env, "SELECT COUNT(*) FROM events WHERE type = 'conversation.state_changed'")[0] == 1
    assert len(runs(env)) == 1  # state change by the rule did not retrigger


def test_webhook_without_secret_has_no_signature_and_tolerates_plain_response(env, conv):
    mk_rule(env, action={"type": "webhook", "url": "http://hook.example/x"})
    env.post_results.append((200, "ok"))
    inbound(env, conv)
    process(env)
    assert "X-WA-Signature" not in env.posts[0]["headers"]
    assert runs(env)[0]["status"] == "done"


def test_webhook_http_error_is_retried(env, conv):
    mk_rule(env, action={"type": "webhook", "url": "http://hook.example/x"})
    env.post_results.append((500, "oops"))
    inbound(env, conv)
    process(env)
    run = runs(env)[0]
    assert run["status"] == "queued" and "HTTP 500" in run["error"]


def test_script_action_gets_json_on_stdin(env, conv):
    mk_rule(env, action={"type": "script", "command": ["/usr/local/bin/handler", "--flag"], "timeout_s": 7})
    env.command_results.append((0, json.dumps({"tags": ["scripted"], "reply": "From script"}), ""))
    inbound(env, conv)
    process(env)
    call = env.commands[0]
    assert call["argv"] == ["/usr/local/bin/handler", "--flag"] and call["timeout"] == 7
    payload = json.loads(call["stdin"])
    assert payload["event"]["type"] == "message.in" and payload["conversation"]["id"] == conv
    assert one(env, "SELECT author FROM messages WHERE status = 'draft'")["author"].startswith("rule:")
    assert "scripted" in json.loads(one(env, "SELECT tags FROM conversations WHERE id = ?", (conv,))["tags"])


def test_reply_action_renders_template_and_sends(env, conv):
    pair_account(env)
    mk_rule(env, action={"type": "reply", "text": "Hello {contact_name}! {missing}Ticket {conversation_id}"}, reply_mode="send")
    inbound(env, conv)
    process(env)
    assert env.bridge_calls[0][3] == {"chatId": CONTACT_JID, "message": f"Hello Anna! Ticket {conv}"}


@pytest.mark.parametrize(
    ("action", "check"),
    [
        ({"type": "set_state", "state": "waiting"}, lambda c: c["state"] == "waiting" and c["previous_state"] == "new"),
        ({"type": "add_tags", "tags": ["vip", "new-lead"]}, lambda c: json.loads(c["tags"]) == ["vip", "new-lead"]),
        ({"type": "escalate"}, lambda c: c["priority"] == 2),
        ({"type": "takeover"}, lambda c: c["state"] == "in_progress" and c["agent_active"] == 0),
    ],
)
def test_conversation_actions(env, conv, action, check):
    mk_rule(env, action=action)
    inbound(env, conv)
    process(env)
    assert runs(env)[0]["status"] == "done"
    assert check(one(env, "SELECT * FROM conversations WHERE id = ?", (conv,)))
    assert env.commands == [] and env.posts == []


# --- Templates --------------------------------------------------------------------------------


def test_render_template_unknown_keys_empty_and_not_reexpanded(env):
    render = env.core.automations.render_template
    assert render("a{nope}b {text}", {"text": "{history}", "history": "H"}) == "ab {history}"
    assert render('json {"a": 1}', {}) == 'json {"a": 1}'
    assert render("", {}) == ""


def test_history_is_limited_to_setting(env, conv):
    set_automation_settings(env, history_messages_in_prompt=2)
    mk_rule(env, action={"type": "hermes", "profile": None, "prompt": "{history}"})
    for i in range(4):
        add_msg(env, conv, f"older {i}", ts=NOW - 100 + i)
    inbound(env, conv, "latest")
    process(env)
    argv = env.commands[0]["argv"]
    lines = argv[argv.index("-q") + 1].splitlines()
    assert len(lines) == 2 and lines[-1].endswith("contact: latest") and lines[0].endswith("contact: older 3")


# --- Dry run ------------------------------------------------------------------------------------


def test_dry_run_renders_prompt_without_side_effects(env, conv):
    rule = mk_rule(env, conditions={"text_contains": ["price"]}, action={"type": "hermes", "profile": None, "prompt": "Answer {contact_name}: {text}"})
    add_msg(env, conv)

    def counts():
        return tuple(one(env, f"SELECT COUNT(*) FROM {t}")[0] for t in ("events", "automation_runs", "messages", "conversation_state_log"))

    before = counts()
    res = env.client.post(f"{PREFIX}/automations/{rule['id']}/test", json={"conversation_id": conv})
    assert res.status_code == 200
    body = res.json()
    assert body["matches"] is True
    assert body["rendered_prompt"] == "Answer Anna: Hello, I would like the PRICE list"
    assert any("price" in r.lower() for r in body["reasons"])
    assert counts() == before and env.commands == [] and env.posts == []


def test_dry_run_reports_why_not(env, conv):
    rule = mk_rule(env, conditions={"states": ["closed"]})
    add_msg(env, conv)
    body = env.client.post(f"{PREFIX}/automations/{rule['id']}/test", json={"conversation_id": conv}).json()
    assert body["matches"] is False
    assert any(r.startswith("no: state is new") for r in body["reasons"])
    assert body["rendered_prompt"] == "Reply to Hello, I would like the PRICE list"


def test_dry_run_loop_guard_and_missing_message(env, conv):
    rule = mk_rule(env)
    body = env.client.post(f"{PREFIX}/automations/{rule['id']}/test", json={"conversation_id": conv}).json()
    assert body["matches"] is False and any("no inbound message" in r for r in body["reasons"])
    add_msg(env, conv, "bot", author="agent:x")
    body = env.client.post(f"{PREFIX}/automations/{rule['id']}/test", json={"conversation_id": conv}).json()
    assert body["matches"] is False and any("loop guard" in r for r in body["reasons"])


def test_dry_run_not_found(env, conv):
    rule = mk_rule(env)
    assert env.client.post(f"{PREFIX}/automations/{rule['id']}/test", json={"conversation_id": 999}).status_code == 404
    assert env.client.post(f"{PREFIX}/automations/999/test", json={"conversation_id": conv}).status_code == 404


# --- CRUD -----------------------------------------------------------------------------------------


def test_crud_roundtrip(env):
    add_account(env)
    created = mk_rule(
        env,
        name="  Greeter ",
        account_id=1,
        event_types=["message.in", "message.in", "conversation.created"],
        conditions={"text_contains": ["hi"], "business_hours": "in"},
        action={"type": "webhook", "url": "https://x.test/h", "secret": None},
        reply_mode="none",
        stop_after_match=True,
    )
    assert created["name"] == "Greeter" and created["position"] == 1 and created["enabled"] is True
    assert created["event_types"] == ["message.in", "conversation.created"]
    assert created["conditions"] == {"text_contains": ["hi"], "business_hours": "in"}
    assert created["action"] == {"type": "webhook", "url": "https://x.test/h", "secret": None, "timeout_s": 30}
    assert created["reply_mode"] == "none" and created["stop_after_match"] is True

    second = mk_rule(env, name="second")
    assert second["position"] == 2
    assert second["action"]["timeout_s"] == 180 and second["action"]["skills"] == []
    assert second["event_types"] == ["message.in"] and second["reply_mode"] == "draft"

    updated = env.client.put(
        f"{PREFIX}/automations/{created['id']}",
        json={"name": "Renamed", "enabled": False, "action": {"type": "escalate"}, "event_types": ["message.out"]},
    ).json()
    assert updated["name"] == "Renamed" and updated["enabled"] is False and updated["action"] == {"type": "escalate"}
    assert updated["position"] == 1 and updated["account_id"] is None and updated["conditions"] == {}

    listed = env.client.get(f"{PREFIX}/automations").json()["rules"]
    assert [r["id"] for r in listed] == [created["id"], second["id"]]

    assert env.client.delete(f"{PREFIX}/automations/{created['id']}").json() == {"ok": True}
    assert [r["id"] for r in env.client.get(f"{PREFIX}/automations").json()["rules"]] == [second["id"]]
    assert env.client.delete(f"{PREFIX}/automations/{created['id']}").status_code == 404
    assert env.client.put(f"{PREFIX}/automations/999", json={"name": "x", "action": {"type": "escalate"}}).status_code == 404


def test_reorder_unknown_id_404_and_partial_list(env):
    a, b, c = (mk_rule(env, name=n) for n in "abc")
    assert env.client.post(f"{PREFIX}/automations/reorder", json={"ids": [999]}).status_code == 404
    res = env.client.post(f"{PREFIX}/automations/reorder", json={"ids": [c["id"]]}).json()["rules"]
    assert [r["id"] for r in res] == [c["id"], a["id"], b["id"]]
    assert [r["position"] for r in res] == [1, 2, 3]


_ACTION = {"type": "escalate"}


@pytest.mark.parametrize(
    "overrides",
    [
        {"name": ""},
        {"event_types": []},
        {"event_types": ["message.draft"]},
        {"event_types": ["nonsense"]},
        {"conditions": {"unknown_key": 1}},
        {"conditions": {"states": ["archived"]}},
        {"conditions": {"business_hours": "maybe"}},
        {"conditions": {"text_regex": "("}},
        {"conditions": {"directions": ["sideways"]}},
        {"action": {"type": "teleport"}},
        {"action": {"type": "hermes"}},
        {"action": {"type": "hermes", "prompt": "x", "timeout_s": 0}},
        {"action": {"type": "hermes", "prompt": "x", "extra": 1}},
        {"action": {"type": "webhook", "url": "ftp://nope"}},
        {"action": {"type": "script", "command": []}},
        {"action": {"type": "set_state", "state": "archived"}},
        {"action": {"type": "add_tags", "tags": []}},
        {"action": {"type": "reply", "text": ""}},
        {"reply_mode": "shout"},
        {"unexpected": True},
    ],
)
def test_rule_validation_422(env, overrides):
    body = {"name": "r", "action": _ACTION, **overrides}
    assert env.client.post(f"{PREFIX}/automations", json=body).status_code == 422
    rule = mk_rule(env)
    assert env.client.put(f"{PREFIX}/automations/{rule['id']}", json=body).status_code == 422
    assert len(env.client.get(f"{PREFIX}/automations").json()["rules"]) == 1


def test_missing_action_is_422_and_unknown_account_is_400(env):
    assert env.client.post(f"{PREFIX}/automations", json={"name": "r"}).status_code == 422
    res = env.client.post(f"{PREFIX}/automations", json={"name": "r", "action": _ACTION, "account_id": 42})
    assert res.status_code == 400 and "account 42" in res.json()["detail"]


# --- Runs API ---------------------------------------------------------------------------------------


def test_runs_listing_filters_and_retry(env, conv):
    add_conv(env, jid="393330009999@s.whatsapp.net")
    other = 2
    r1 = mk_rule(env, name="first")
    inbound(env, conv)
    inbound(env, other)
    env.command_results.extend([(1, "", "boom")] * 6)  # both runs fail at each of the three attempts
    process(env, NOW)
    process(env, NOW + 30)
    process(env, NOW + 150)

    everything = env.client.get(f"{PREFIX}/automation-runs").json()["runs"]
    assert [r["id"] for r in everything] == [2, 1]  # newest first
    assert everything[0]["rule_name"] == "first"
    assert [r["id"] for r in env.client.get(f"{PREFIX}/automation-runs?conversation_id={conv}").json()["runs"]] == [1]
    assert len(env.client.get(f"{PREFIX}/automation-runs?rule_id={r1['id']}&limit=1").json()["runs"]) == 1
    assert env.client.get(f"{PREFIX}/automation-runs?rule_id=999").json() == {"runs": []}
    assert env.client.get(f"{PREFIX}/automation-runs?limit=0").status_code == 422

    failed = next(r for r in everything if r["status"] == "failed")
    retried = env.client.post(f"{PREFIX}/automation-runs/{failed['id']}/retry").json()
    assert retried["status"] == "queued" and retried["attempts"] == 0 and retried["error"] is None
    env.command_results.append((0, "better now", ""))
    assert process(env, NOW + 200) >= 1
    assert env.client.get(f"{PREFIX}/automation-runs?conversation_id={failed['conversation_id']}").json()["runs"][0]["status"] == "done"


def test_retry_running_is_409_and_unknown_is_404(env, conv):
    mk_rule(env)
    inbound(env, conv)
    env.conn.execute("UPDATE automation_runs SET status = 'running', started_at = ?", (NOW,))
    assert env.client.post(f"{PREFIX}/automation-runs/1/retry").status_code == 409
    assert env.client.post(f"{PREFIX}/automation-runs/999/retry").status_code == 404
