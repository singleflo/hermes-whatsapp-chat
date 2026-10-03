"""New chat: number checks and first messages to numbers with no conversation yet.

Loads the REAL plugin/dashboard/plugin_api.py (and the REAL plugin/scripts/wa.py) against a throwaway SQLite DB.
Only the WhatsApp bridge (``core.bridge.bridge_request``) is faked.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = REPO_ROOT / "plugin" / "dashboard" / "plugin_api.py"
CLI_FILE = REPO_ROOT / "plugin" / "scripts" / "wa.py"
PREFIX = "/api/plugins/hermes-whatsapp-chat"
OWN = "393990000000"
PHONE = "393331234567"
NOW = int(time.time())


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- Fixtures ----------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WA_ARCHIVE_DB", str(tmp_path / "wa_board.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.delenv("HERMES_PROFILE", raising=False)
    plugin = _load("hermes_dashboard_plugin_hermes_whatsapp_chat_newchat", PLUGIN_FILE)
    core = plugin.core
    app = FastAPI()
    app.include_router(plugin.router, prefix=PREFIX)

    fake = SimpleNamespace(
        calls=[],
        missing=set(),  # digits that are not on WhatsApp
        jids={},  # digits -> jid the bridge answers with (default <digits>@s.whatsapp.net)
        down=False,
        check_reply=None,  # (status, body) overriding the /check answer
        send_reply=(200, {"success": True, "messageId": "WAID1"}),
    )

    def bridge_request(port, method, path, payload=None, *, timeout):
        fake.calls.append((port, method, path, payload))
        if fake.down:
            raise core.bridge.BridgeUnavailable("no bridge in tests")
        if path == "/check":
            if fake.check_reply is not None:
                return fake.check_reply
            results = [
                {
                    "phone": p,
                    "exists": p not in fake.missing,
                    "jid": None if p in fake.missing else fake.jids.get(p, f"{p}@s.whatsapp.net"),
                }
                for p in (payload or {})["phones"]
            ]
            return 200, {"results": results}
        if path == "/send":
            return fake.send_reply
        raise core.bridge.BridgeUnavailable(f"unexpected path {path}")

    monkeypatch.setattr(core.bridge, "bridge_request", bridge_request)
    conn = core.db.connect()
    yield SimpleNamespace(
        core=core,
        client=TestClient(app),
        conn=conn,
        bridge=fake,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        fn=bridge_request,
    )
    conn.close()


def add_account(
    env, label="Main", *, kind="whatsapp", desired="running", paired=True, phone: str | None = OWN
) -> int:
    cur = env.conn.execute(
        "INSERT INTO accounts (kind, label, color, desired, phone, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
        (kind, label, "green", desired, phone, NOW, NOW),
    )
    account_id = int(cur.lastrowid or 0)
    if kind == "whatsapp":
        env.conn.execute(
            "UPDATE accounts SET port = ?, session_dir = ? WHERE id = ?",
            (4000 + account_id, f"sessions/{account_id}", account_id),
        )
        if paired:
            session = env.core.db.data_dir() / f"sessions/{account_id}"
            session.mkdir(parents=True, exist_ok=True)
            (session / "creds.json").write_text("{}", encoding="utf-8")
    return account_id


def add_conv(env, account_id, jid, *, name=None, phone=None, state="waiting") -> int:
    cur = env.conn.execute(
        "INSERT INTO conversations (account_id, chat_jid, contact_name, phone, state, created_at, updated_at,"
        " last_message_at) VALUES (?,?,?,?,?,?,?,?)",
        (account_id, jid, name, phone, state, NOW - 100, NOW - 100, NOW - 100),
    )
    return int(cur.lastrowid or 0)


def start(env, *, phone=PHONE, text="Hello!", mode="send", account_id=None, name=None, now=NOW):
    return env.core.newchat.start_conversation(
        env.conn,
        phone=phone,
        text=text,
        mode=mode,
        account_id=account_id,
        name=name,
        author="user",
        actor="user",
        now=now,
    )


def checks(env):
    return [c for c in env.bridge.calls if c[2] == "/check"]


def rows(env, sql, *args):
    return [dict(r) for r in env.conn.execute(sql, args)]


# --- normalize_phone ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    ["+39 333 123 4567", "0039 333 1234567", "393331234567", "+39-333.123.4567", "(+39) 333/1234567", " +393331234567 "],
)
def test_normalize_phone_accepts_international_forms(env, raw):
    assert env.core.newchat.normalize_phone(raw) == PHONE


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        ("0333 1234567", "looks like a national number, add the country code (e.g. +39 333 1234567)"),
        ("3331234567", "add the country code (e.g. +39 333 1234567)"),
        ("+39 abc", "is not a valid phone number"),
        ("+123", "is not a valid phone number"),
        ("+1234567890123456", "is not a valid phone number"),
        ("", "is not a valid phone number"),
        ("12345678901x", "is not a valid phone number"),
    ],
)
def test_normalize_phone_rejects(env, raw, message):
    with pytest.raises(env.core.errors.Invalid) as exc:
        env.core.newchat.normalize_phone(raw)
    assert message in str(exc.value) and str(exc.value).startswith(raw)


# --- pick_account ------------------------------------------------------------------------


def test_pick_account_single_linked_number_is_the_default(env):
    add_account(env, "Main")
    add_account(env, "Stopped", desired="stopped")
    add_account(env, "Unpaired", paired=False)
    add_account(env, "Demo", kind="demo")
    assert env.core.newchat.pick_account(env.conn, None)["label"] == "Main"


def test_pick_account_explicit_wins_when_several_are_linked(env):
    add_account(env, "Main")
    second = add_account(env, "Second")
    assert env.core.newchat.pick_account(env.conn, second)["label"] == "Second"


def test_pick_account_none_linked_is_unavailable(env):
    add_account(env, "Stopped", desired="stopped")
    with pytest.raises(env.core.errors.Unavailable, match="no WhatsApp number is linked and running"):
        env.core.newchat.pick_account(env.conn, None)


def test_pick_account_several_linked_asks_to_choose(env):
    add_account(env, "Main")
    add_account(env, "Second")
    with pytest.raises(env.core.errors.Invalid, match=r"several WhatsApp numbers are linked: choose one"):
        env.core.newchat.pick_account(env.conn, None)


def test_pick_account_explicit_errors(env):
    demo = add_account(env, "Demo", kind="demo")
    stopped = add_account(env, "Stopped", desired="stopped")
    unpaired = add_account(env, "Unpaired", paired=False)
    pick = env.core.newchat.pick_account
    with pytest.raises(env.core.errors.NotFound):
        pick(env.conn, 99)
    with pytest.raises(env.core.errors.Invalid, match="demo numbers can never send messages"):
        pick(env.conn, demo)
    for account_id, label in ((stopped, "Stopped"), (unpaired, "Unpaired")):
        with pytest.raises(env.core.errors.Unavailable, match=f"account '{label}' is not paired and running"):
            pick(env.conn, account_id)


# --- check_numbers -----------------------------------------------------------------------


def test_check_numbers_reports_exists_self_and_existing_conversation(env):
    account = add_account(env)
    known = add_conv(env, account, f"{PHONE}@s.whatsapp.net", phone=PHONE)
    env.bridge.missing.add("393339999999")
    res = env.core.newchat.check_numbers(env.conn, ["+39 333 123 4567", "+393339999999", f"+{OWN}"])
    assert res["account_id"] == account
    first, missing, own = res["results"]
    assert first == {
        "input": "+39 333 123 4567",
        "phone": f"+{PHONE}",
        "exists": True,
        "jid": f"{PHONE}@s.whatsapp.net",
        "self": False,
        "conversation_id": known,
    }
    assert missing["exists"] is False and missing["jid"] is None and missing["conversation_id"] is None
    assert own["self"] is True and own["exists"] is True
    assert checks(env)[0][3] == {"phones": [PHONE, "393339999999", OWN]}
    assert checks(env)[0][1] == "POST"


def test_check_numbers_finds_conversation_by_the_returned_jid(env):
    account = add_account(env)
    env.bridge.jids[PHONE] = "551187654321@s.whatsapp.net"
    known = add_conv(env, account, "551187654321@s.whatsapp.net", phone="551187654321")
    res = env.core.newchat.check_numbers(env.conn, [PHONE])
    assert res["results"][0]["conversation_id"] == known


def test_check_numbers_validates_input_before_calling_the_bridge(env):
    add_account(env)
    check = env.core.newchat.check_numbers
    with pytest.raises(env.core.errors.Invalid):
        check(env.conn, [])
    with pytest.raises(env.core.errors.Invalid):
        check(env.conn, [f"+39333{i:07d}" for i in range(51)])
    with pytest.raises(env.core.errors.Invalid, match="national number"):
        check(env.conn, ["+393331234567", "0333 1234567"])
    assert env.bridge.calls == []


def test_check_numbers_bridge_unreachable_is_503(env):
    add_account(env, "Main")
    env.bridge.down = True
    with pytest.raises(env.core.errors.Unavailable, match="WhatsApp bridge of 'Main' is not reachable"):
        env.core.newchat.check_numbers(env.conn, [PHONE])


def test_check_numbers_bridge_errors_map_to_503_and_502(env):
    add_account(env)
    env.bridge.check_reply = (503, {"error": "not connected"})
    with pytest.raises(env.core.errors.Unavailable, match="bridge check failed: not connected"):
        env.core.newchat.check_numbers(env.conn, [PHONE])
    env.bridge.check_reply = (500, {"error": "boom"})
    with pytest.raises(env.core.errors.BadGateway, match="bridge check failed: boom"):
        env.core.newchat.check_numbers(env.conn, [PHONE])


# --- start_conversation ------------------------------------------------------------------


def test_start_sends_and_creates_an_in_progress_conversation(env):
    account = add_account(env)
    rule = env.client.post(
        f"{PREFIX}/automations",
        json={"name": "on created", "event_types": ["conversation.created"], "action": {"type": "escalate"}},
    )
    assert rule.status_code == 200, rule.text

    res = start(env, name="  Anna  ")
    conv = res["conversation"]
    assert res["created"] is True
    assert conv["account_id"] == account and conv["contact_name"] == "Anna"
    assert conv["phone"] == PHONE and conv["chat_jid"] == f"{PHONE}@s.whatsapp.net"
    assert conv["state"] == "waiting"  # the outbound rule, as for any reply
    assert res["message"]["status"] == "sent" and res["message"]["author"] == "user"
    assert res["message"]["conversation_id"] == conv["id"] and res["message"]["body"] == "Hello!"
    sent = [c for c in env.bridge.calls if c[2] == "/send"]
    assert sent[0][3] == {"chatId": f"{PHONE}@s.whatsapp.net", "message": "Hello!"}
    assert [c[2] for c in env.bridge.calls] == ["/check", "/send"]

    log = rows(env, "SELECT from_state, to_state, actor, reason, at FROM conversation_state_log ORDER BY id")
    assert log[0] == {
        "from_state": None,
        "to_state": "in_progress",
        "actor": "user",
        "reason": "conversation started",
        "at": NOW,
    }
    assert (log[1]["from_state"], log[1]["to_state"], log[1]["actor"]) == ("in_progress", "waiting", "auto")
    events = rows(env, "SELECT type, payload FROM events WHERE conversation_id = ? ORDER BY id", conv["id"])
    types = [e["type"] for e in events]
    assert "conversation.created" not in types and types[0] == "conversation.updated"
    assert json.loads(events[0]["payload"]) == {"fields": ["created"], "origin": "outbound"}
    assert rows(env, "SELECT COUNT(*) AS n FROM automation_runs")[0]["n"] == 0
    assert conv["unread_count"] == 0


def test_start_draft_stays_in_progress_and_never_sends(env):
    add_account(env)
    res = start(env, mode="draft", text="  Hi there  ")
    assert res["created"] is True
    assert res["conversation"]["state"] == "in_progress"
    assert res["message"]["status"] == "draft" and res["message"]["body"] == "Hi there"
    assert [c[2] for c in env.bridge.calls] == ["/check"]


def test_start_reuses_an_existing_conversation_without_checking(env):
    account = add_account(env)
    known = add_conv(env, account, f"{PHONE}@s.whatsapp.net", name="Anna", phone=PHONE, state="closed")
    res = start(env, name="Someone else")
    assert res["created"] is False and res["conversation"]["id"] == known
    assert res["conversation"]["contact_name"] == "Anna"
    assert res["message"]["status"] == "sent"
    assert checks(env) == []
    assert rows(env, "SELECT COUNT(*) AS n FROM conversations")[0]["n"] == 1
    assert rows(env, "SELECT COUNT(*) AS n FROM conversation_state_log WHERE reason = 'conversation started'")[0]["n"] == 0


def test_start_reuses_a_conversation_matched_by_phone_column(env):
    account = add_account(env)
    known = add_conv(env, account, "123456789@lid", phone=PHONE)
    res = start(env, mode="draft")
    assert res["created"] is False and res["conversation"]["id"] == known and checks(env) == []


def test_start_uses_the_jid_the_bridge_returns(env):
    add_account(env)
    digits = "5511987654321"
    env.bridge.jids[digits] = "551187654321@s.whatsapp.net"  # Brazilian mobile numbers lose the 9
    first = start(env, phone=f"+{digits}", mode="draft")
    assert first["created"] is True
    assert first["conversation"]["chat_jid"] == "551187654321@s.whatsapp.net"
    assert first["conversation"]["phone"] == "551187654321"
    again = start(env, phone=f"+{digits}", mode="draft")
    assert again["created"] is False and again["conversation"]["id"] == first["conversation"]["id"]
    assert len(checks(env)) == 2  # the number itself is not stored, so the bridge is asked again
    assert rows(env, "SELECT COUNT(*) AS n FROM conversations")[0]["n"] == 1


def test_start_not_on_whatsapp_creates_nothing(env):
    add_account(env)
    env.bridge.missing.add(PHONE)
    with pytest.raises(env.core.errors.NotOnWhatsApp, match=r"^\+393331234567 is not on WhatsApp$"):
        start(env)
    assert rows(env, "SELECT COUNT(*) AS n FROM conversations")[0]["n"] == 0
    assert rows(env, "SELECT COUNT(*) AS n FROM conversation_state_log")[0]["n"] == 0
    assert rows(env, "SELECT COUNT(*) AS n FROM messages")[0]["n"] == 0
    assert env.core.errors.NotOnWhatsApp.status == 404


def test_start_refuses_the_accounts_own_number(env):
    add_account(env)
    with pytest.raises(env.core.errors.Invalid, match=rf"^\+{OWN} is this account's own number$"):
        start(env, phone=f"+{OWN}")
    assert env.bridge.calls == []


def test_start_own_number_falls_back_to_the_jid(env):
    account = add_account(env, phone=None)
    env.conn.execute("UPDATE accounts SET wa_jid = ? WHERE id = ?", (f"{OWN}:7@s.whatsapp.net", account))
    with pytest.raises(env.core.errors.Invalid, match="own number"):
        start(env, phone=f"+{OWN}")


def test_start_rejects_empty_text_and_demo_account(env):
    account = add_account(env)
    demo = add_account(env, "Demo", kind="demo")
    with pytest.raises(env.core.errors.Invalid, match="message text is empty"):
        start(env, text="   ")
    with pytest.raises(env.core.errors.Invalid, match="demo numbers"):
        start(env, account_id=demo)
    assert account and env.bridge.calls == []


def test_start_failed_send_leaves_the_conversation_with_its_failed_message(env):
    add_account(env)
    env.bridge.send_reply = (500, {"error": "nope"})
    with pytest.raises(env.core.errors.BadGateway):
        start(env)
    conv = rows(env, "SELECT id, state FROM conversations")
    assert len(conv) == 1 and conv[0]["state"] == "in_progress"
    msgs = rows(env, "SELECT status FROM messages WHERE conversation_id = ?", conv[0]["id"])
    assert [m["status"] for m in msgs] == ["failed"]


def test_start_rate_limit_is_ten_new_chats_per_hour_per_number(env):
    account = add_account(env)
    other = add_account(env, "Second", phone="393880000000")
    for i in range(10):
        start(env, phone=f"+39333123{i:04d}", mode="draft", account_id=account)
    with pytest.raises(env.core.errors.TooMany, match=r"at most 10 new chats per hour per number") as exc:
        start(env, phone="+393331239999", mode="draft", account_id=account)
    assert "try again later" in str(exc.value) and env.core.errors.TooMany.status == 429
    assert rows(env, "SELECT COUNT(*) AS n FROM conversations")[0]["n"] == 10
    # an existing conversation is not a new chat; another number has its own budget; an hour later it is free again
    assert start(env, phone="+393331230000", account_id=account)["created"] is False
    assert start(env, phone="+393331239999", mode="draft", account_id=other)["created"] is True
    assert start(env, phone="+393331238888", mode="draft", account_id=account, now=NOW + 3601)["created"] is True


# --- Routes ------------------------------------------------------------------------------


def test_route_check_numbers(env):
    add_account(env)
    env.bridge.missing.add("393339999999")
    res = env.client.post(f"{PREFIX}/contacts/check", json={"phones": ["+39 333 123 4567", "+393339999999"]})
    assert res.status_code == 200, res.text
    body = res.json()
    assert [r["exists"] for r in body["results"]] == [True, False] and body["account_id"] == 1


def test_route_check_validation_and_errors(env):
    add_account(env)
    post = env.client.post
    assert post(f"{PREFIX}/contacts/check", json={"phones": []}).status_code == 422
    assert post(f"{PREFIX}/contacts/check", json={"phones": [f"+39333{i:07d}" for i in range(51)]}).status_code == 422
    res = post(f"{PREFIX}/contacts/check", json={"phones": ["0333 1234567"]})
    assert res.status_code == 400 and "national number" in res.json()["detail"]
    env.bridge.down = True
    res = post(f"{PREFIX}/contacts/check", json={"phones": [PHONE]})
    assert res.status_code == 503 and res.json()["detail"] == "WhatsApp bridge of 'Main' is not reachable"


def test_route_new_conversation_defaults_to_a_draft(env):
    add_account(env)
    res = env.client.post(f"{PREFIX}/conversations", json={"phone": "+39 333 123 4567", "text": "Hi", "name": "Anna"})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["created"] is True and body["message"]["status"] == "draft"
    assert body["conversation"]["state"] == "in_progress" and body["conversation"]["contact_name"] == "Anna"
    log = rows(env, "SELECT actor FROM conversation_state_log WHERE reason = 'conversation started'")
    assert log == [{"actor": "user"}]


def test_route_new_conversation_send_and_errors(env):
    add_account(env)
    ok = env.client.post(f"{PREFIX}/conversations", json={"phone": PHONE, "text": "Hi", "mode": "send"})
    assert ok.status_code == 200 and ok.json()["conversation"]["state"] == "waiting"
    assert ok.json()["message"]["author"] == "user"
    post = env.client.post
    env.bridge.missing.add("393339999999")
    res = post(f"{PREFIX}/conversations", json={"phone": "+393339999999", "text": "Hi"})
    assert res.status_code == 404 and res.json()["detail"] == "+393339999999 is not on WhatsApp"
    assert post(f"{PREFIX}/conversations", json={"phone": f"+{OWN}", "text": "Hi"}).status_code == 400
    assert post(f"{PREFIX}/conversations", json={"phone": PHONE, "text": ""}).status_code == 422
    assert post(f"{PREFIX}/conversations", json={"phone": PHONE, "text": "x", "mode": "bulk"}).status_code == 422
    assert post(f"{PREFIX}/conversations", json={"phone": PHONE, "text": "x" * 4097}).status_code == 422
    assert post(f"{PREFIX}/conversations", json={"phone": PHONE, "text": "x", "name": "n" * 121}).status_code == 422


def test_route_new_conversation_rate_limit_is_429(env):
    add_account(env)
    for i in range(10):
        res = env.client.post(f"{PREFIX}/conversations", json={"phone": f"+39333123{i:04d}", "text": "Hi"})
        assert res.status_code == 200, res.text
    res = env.client.post(f"{PREFIX}/conversations", json={"phone": "+393331239999", "text": "Hi"})
    assert res.status_code == 429 and "at most 10 new chats per hour" in res.json()["detail"]


def test_route_new_conversation_without_linked_number_is_503(env):
    res = env.client.post(f"{PREFIX}/conversations", json={"phone": PHONE, "text": "Hi"})
    assert res.status_code == 503 and res.json()["detail"] == "no WhatsApp number is linked and running"


# --- CLI ---------------------------------------------------------------------------------


@pytest.fixture
def cli(env):
    mod = _load("hwc_wa_cli_newchat_test", CLI_FILE)
    env.monkeypatch.setattr(mod.core.bridge, "bridge_request", env.fn)
    return SimpleNamespace(main=mod.main)


def wa(cli, capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_check_exit_codes_and_lines(env, cli, capsys):
    account = add_account(env)
    known = add_conv(env, account, f"{PHONE}@s.whatsapp.net", phone=PHONE)
    code, out, _ = wa(cli, capsys, "check", "+39 333 123 4567")
    assert code == 0
    assert out.strip() == f"+{PHONE}  on WhatsApp  ({PHONE}@s.whatsapp.net)  conversation #{known}"
    env.bridge.missing.add("393339999999")
    code, out, _ = wa(cli, capsys, "check", "+393339999999", f"+{OWN}")
    assert code == 2
    lines = out.strip().splitlines()
    assert lines[0] == "+393339999999  NOT on WhatsApp"
    assert lines[1] == f"+{OWN}  on WhatsApp  ({OWN}@s.whatsapp.net)  (this number)"
    code, out, _ = wa(cli, capsys, "check", PHONE, "--json")
    assert code == 0 and json.loads(out)["results"][0]["conversation_id"] == known


def test_cli_check_errors_exit_1(env, cli, capsys):
    code, out, err = wa(cli, capsys, "check", "+393331234567")  # no number linked
    assert code == 1 and out == "" and err.startswith("error: no WhatsApp number is linked")
    add_account(env)
    code, _, err = wa(cli, capsys, "check", "0333 1234567")
    assert code == 1 and "national number" in err


def test_cli_send_to_and_draft_to(env, cli, capsys, monkeypatch):
    add_account(env)
    monkeypatch.setenv("HERMES_PROFILE", "sales")
    code, out, err = wa(cli, capsys, "send-to", "+39 333 123 4567", "Ciao!", "--name", "Anna")
    assert code == 0, err
    assert out.strip().endswith("(new conversation)") and out.startswith("sent #1 to conversation 1 ")
    msg = rows(env, "SELECT author, status, body FROM messages WHERE id = 1")[0]
    assert msg == {"author": "agent:sales", "status": "sent", "body": "Ciao!"}
    assert rows(env, "SELECT contact_name, state FROM conversations")[0] == {"contact_name": "Anna", "state": "waiting"}
    assert rows(env, "SELECT actor FROM conversation_state_log WHERE reason = 'conversation started'")[0]["actor"] == (
        "agent:sales"
    )

    code, out, _ = wa(cli, capsys, "draft-to", PHONE, "Second thought")
    assert code == 0 and out.strip() == "draft #2 in conversation 1 (existing conversation)"
    assert rows(env, "SELECT status FROM messages WHERE id = 2")[0]["status"] == "draft"

    code, out, _ = wa(cli, capsys, "draft-to", "+393331230001", "Hi", "--json")
    data = json.loads(out)
    assert code == 0 and data["created"] is True and data["message"]["status"] == "draft"
    assert data["conversation"]["state"] == "in_progress"


def test_cli_send_to_not_on_whatsapp_exits_2(env, cli, capsys):
    add_account(env)
    env.bridge.missing.add(PHONE)
    code, out, err = wa(cli, capsys, "send-to", PHONE, "Hi")
    assert code == 2 and out == "" and err.strip() == f"error: +{PHONE} is not on WhatsApp"
    assert rows(env, "SELECT COUNT(*) AS n FROM conversations")[0]["n"] == 0
    assert wa(cli, capsys, "draft-to", PHONE, "Hi")[0] == 2


def test_cli_other_errors_exit_1(env, cli, capsys):
    add_account(env)
    code, _, err = wa(cli, capsys, "send-to", f"+{OWN}", "Hi")
    assert code == 1 and "own number" in err
    code, _, err = wa(cli, capsys, "send-to", PHONE, "   ")
    assert code == 1 and "message text is empty" in err


def test_cli_account_accepts_id_or_label(env, cli, capsys):
    add_account(env, "Main")
    second = add_account(env, "Sales Line", phone="393880000000")
    code, _, err = wa(cli, capsys, "check", PHONE)  # two numbers: must choose
    assert code == 1 and "several WhatsApp numbers are linked" in err
    code, out, _ = wa(cli, capsys, "check", PHONE, "--account", "sales line", "--json")
    assert code == 0 and json.loads(out)["account_id"] == second
    code, out, _ = wa(cli, capsys, "check", PHONE, "--account", str(second), "--json")
    assert code == 0 and json.loads(out)["account_id"] == second
    code, out, _ = wa(cli, capsys, "send-to", PHONE, "Hi", "--account", "SALES LINE")
    assert code == 0 and "(new conversation)" in out
    assert rows(env, "SELECT account_id FROM conversations") == [{"account_id": second}]
    code, out, _ = wa(cli, capsys, "draft-to", "+393331230002", "Hi", "--account", "main")
    assert code == 0 and rows(env, "SELECT account_id FROM conversations WHERE phone = '393331230002'")[0] == {
        "account_id": 1
    }


def test_cli_unknown_account_label_exits_1(env, cli, capsys):
    add_account(env)
    for argv in (("check", PHONE), ("send-to", PHONE, "Hi"), ("draft-to", PHONE, "Hi")):
        code, out, err = wa(cli, capsys, *argv, "--account", "nope")
        assert code == 1 and out == "" and err.strip() == "error: unknown account: nope"
    assert env.bridge.calls == []
