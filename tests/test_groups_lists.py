"""WhatsApp groups as conversations, contact lists and silence.

Loads the REAL plugin/dashboard/plugin_api.py (and the REAL plugin/scripts/wa.py) against a throwaway SQLite DB.
The only faked boundary is the WhatsApp bridge (``core.bridge.bridge_request``).
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
SEED_FILE = REPO_ROOT / "plugin" / "scripts" / "seed_demo.py"
PREFIX = "/api/plugins/hermes-whatsapp-chat"
GROUP = "120363000000000001@g.us"
GROUP2 = "120363000000000002@g.us"
CONTACT_JID = "393330001111@s.whatsapp.net"
OWN_JID = "393990000000@s.whatsapp.net"
ALICE = "393201110001@s.whatsapp.net"
BOB = "393201110002@s.whatsapp.net"
NOW = int(time.time())


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("WA_ARCHIVE_DB", str(tmp_path / "wa_board.db"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    plugin = _load("hermes_dashboard_plugin_hermes_whatsapp_chat_groups", PLUGIN_FILE)
    core = plugin.core
    app = FastAPI()
    app.include_router(plugin.router, prefix=PREFIX)

    calls: list[tuple] = []
    replies: dict = {}

    def fake_bridge(port, method, path, payload=None, *, timeout):
        calls.append((port, method, path, payload))
        if path not in replies:
            raise core.bridge.BridgeUnavailable("no bridge in tests")
        return replies[path]

    monkeypatch.setattr(core.bridge, "bridge_request", fake_bridge)
    conn = core.db.connect()
    yield SimpleNamespace(
        core=core, client=TestClient(app), conn=conn, calls=calls, replies=replies, tmp_path=tmp_path,
        monkeypatch=monkeypatch,
    )
    conn.close()


# --- Helpers ----------------------------------------------------------------------------------


def add_account(env, label="Main", *, kind="whatsapp", wa_jid=OWN_JID, **cols) -> int:
    cur = env.conn.execute(
        "INSERT INTO accounts (kind, label, color, desired, wa_jid, phone, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (kind, label, "green", "running", wa_jid, wa_jid.split("@")[0], NOW, NOW),
    )
    account_id = int(cur.lastrowid or 0)
    if kind == "whatsapp":
        env.conn.execute(
            "UPDATE accounts SET port = ?, session_dir = ? WHERE id = ?",
            (4000 + account_id, f"sessions/{account_id}", account_id),
        )
        session = env.core.db.data_dir() / f"sessions/{account_id}"
        session.mkdir(parents=True, exist_ok=True)
        (session / "creds.json").write_text("{}", encoding="utf-8")
    for key, value in cols.items():
        env.conn.execute(f"UPDATE accounts SET {key} = ? WHERE id = ?", (value, account_id))
    return account_id


def event(**overrides) -> dict:
    base = {
        "messageId": "M1", "chatId": CONTACT_JID, "senderId": CONTACT_JID, "senderName": "Mario", "isGroup": False,
        "body": "hello", "hasMedia": False, "mediaType": "", "mediaUrls": [], "botIds": [OWN_JID],
        "fromOwner": False, "timestamp": NOW, "participant": "",
    }
    base.update(overrides)
    return base


def group_event(**overrides) -> dict:
    fields = {
        "messageId": "GM1", "chatId": GROUP, "senderId": ALICE, "senderName": "Alice Martin", "isGroup": True,
        "participant": ALICE,
    }
    fields.update(overrides)
    return event(**fields)


def ingest(env, account_id: int, ev: dict, *, source: str = "live", now: int = NOW):
    return env.core.ingest.ingest_event(env.conn, account_id, ev, now, source=source)


def conv(env, account_id: int, jid: str):
    return env.conn.execute(
        "SELECT * FROM conversations WHERE account_id = ? AND chat_jid = ?", (account_id, jid)
    ).fetchone()


def payloads(env, type_: str) -> list[dict]:
    return [
        json.loads(r[0]) if r[0] else {}
        for r in env.conn.execute("SELECT payload FROM events WHERE type = ? ORDER BY id", (type_,))
    ]


def list_of(env, jid: str):
    row = env.conn.execute("SELECT list FROM contact_lists WHERE jid = ?", (jid,)).fetchone()
    return row[0] if row else None


def get_json(env, path: str, **params):
    res = env.client.get(f"{PREFIX}{path}", params=params)
    assert res.status_code == 200, res.text
    return res.json()


# --- Migration -------------------------------------------------------------------------------


def test_v5_database_migrates_to_v6_marking_groups_and_listing_every_chat(env, monkeypatch):
    path = env.tmp_path / "v5.db"
    monkeypatch.setenv("WA_ARCHIVE_DB", str(path))
    conn = env.core.db.connect()
    conn.execute(
        "INSERT INTO accounts (id, kind, label, port, session_dir, created_at) VALUES (1,'whatsapp','Main',3017,'s',?)",
        (NOW,),
    )
    for conv_id, jid in ((1, CONTACT_JID), (2, GROUP)):
        conn.execute(
            "INSERT INTO conversations (id, account_id, chat_jid, state, created_at) VALUES (?,1,?,'new',?)",
            (conv_id, jid, NOW),
        )
    conn.execute(
        "INSERT INTO messages (conversation_id, account_id, direction, author, body, ts, status)"
        " VALUES (1,1,'in','contact','ciao',?,'received')",
        (NOW,),
    )
    # an installation of the previous release: no v6 columns, no contact_lists
    conn.execute("DROP TABLE contact_lists")
    for column in ("is_group", "list_override", "silenced_until", "participants", "group_refreshed_at"):
        conn.execute(f"ALTER TABLE conversations DROP COLUMN {column}")
    for column in ("new_contact_list", "new_group_list"):
        conn.execute(f"ALTER TABLE accounts DROP COLUMN {column}")
    for column in ("sender_jid", "sender_name", "participant"):
        conn.execute(f"ALTER TABLE messages DROP COLUMN {column}")
    conn.execute("PRAGMA user_version = 5")
    conn.close()

    migrated = env.core.db.connect()
    try:
        assert migrated.execute("PRAGMA user_version").fetchone()[0] == 6
        flags = dict(migrated.execute("SELECT chat_jid, is_group FROM conversations").fetchall())
        assert flags == {CONTACT_JID: 0, GROUP: 1}
        lists = dict(migrated.execute("SELECT jid, list FROM contact_lists").fetchall())
        assert lists == {CONTACT_JID: "unclassified", GROUP: "unclassified"}
        account = migrated.execute("SELECT new_contact_list, new_group_list FROM accounts").fetchone()
        assert tuple(account) == ("unclassified", "unclassified")
        assert migrated.execute("SELECT body, sender_jid FROM messages").fetchone()[:] == ("ciao", None)
    finally:
        migrated.close()


# --- Ingest: groups are conversations ---------------------------------------------------------


def test_group_message_creates_a_group_conversation_keyed_by_the_group(env):
    account = add_account(env)
    message_id = ingest(env, account, group_event())
    assert message_id
    row = conv(env, account, GROUP)
    assert (row["is_group"], row["state"], row["unread_count"], row["phone"]) == (1, "new", 1, None)
    assert row["contact_name"] is None  # the subject comes from group metadata, never from a sender's name
    assert conv(env, account, ALICE) is None
    msg = env.conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
    assert (msg["author"], msg["sender_jid"], msg["sender_name"], msg["participant"]) == (
        "contact", ALICE, "Alice Martin", ALICE,
    )
    created = payloads(env, "conversation.created")[0]
    assert created == {"is_group": True, "list": "unclassified", "silenced": False}
    assert payloads(env, "message.in")[0]["is_group"] is True


def test_group_sender_resolves_lid_to_phone_and_numeric_names_are_dropped(env):
    account = add_account(env)
    session = env.core.db.data_dir() / "sessions" / str(account)
    (session / "lid-mapping-777_reverse.json").write_text('"393201110002"', encoding="utf-8")
    ingest(env, account, group_event(senderId="777@lid", participant="777@lid", senderName="+393201110002"))
    msg = env.conn.execute("SELECT * FROM messages").fetchone()
    assert (msg["sender_jid"], msg["sender_name"], msg["participant"]) == (BOB, None, "777@lid")
    out = get_json(env, f"/conversations/{conv(env, account, GROUP)['id']}/messages")["messages"][0]
    assert (out["sender_phone"], out["sender_name"]) == ("393201110002", None)


def test_sender_name_never_names_the_group_and_contact_updates_do_not_rename_it(env):
    account = add_account(env)
    ingest(env, account, group_event(messageId="G1"))
    ingest(env, account, group_event(messageId="G2", senderId=BOB, participant=BOB, senderName="Bob"))
    row = conv(env, account, GROUP)
    assert row["contact_name"] is None and row["unread_count"] == 2
    item = {"type": "contact", "jid": GROUP, "name": "Somebody"}
    assert env.core.ingest.ingest_update(env.conn, account, item, NOW) == "ignored"
    assert conv(env, account, GROUP)["contact_name"] is None


def test_group_history_is_imported_closed_without_events(env):
    account = add_account(env)
    ingest(env, account, group_event(messageId="H1"), source="history")
    row = conv(env, account, GROUP)
    assert (row["is_group"], row["state"], row["unread_count"]) == (1, "closed", 0)
    msg = env.conn.execute("SELECT * FROM messages").fetchone()
    assert (msg["source"], msg["sender_name"], msg["sender_jid"]) == ("history", "Alice Martin", ALICE)
    assert msg["read_at"] == NOW
    assert env.conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0
    assert list_of(env, GROUP) == "unclassified"


def test_owner_message_in_a_group_is_authored_by_the_phone_even_right_after_an_inbound(env):
    account = add_account(env)
    ingest(env, account, group_event(messageId="G1"))
    ingest(
        env, account,
        group_event(messageId="G2", fromOwner=True, senderId=OWN_JID, participant="", senderName="Me", timestamp=NOW + 1),
    )
    msg = env.conn.execute("SELECT * FROM messages WHERE wa_id = 'G2'").fetchone()
    assert (msg["author"], msg["direction"], msg["sender_jid"], msg["sender_name"]) == ("phone", "out", OWN_JID, None)
    assert conv(env, account, GROUP)["state"] == "waiting"  # the usual outbound rule


def test_owner_message_can_create_the_group_conversation(env):
    account = add_account(env)
    ingest(env, account, group_event(fromOwner=True, senderId=OWN_JID, participant=""))
    row = conv(env, account, GROUP)
    assert (row["is_group"], row["state"]) == (1, "waiting")


def test_broadcast_status_and_newsletter_chats_are_still_dropped(env):
    account = add_account(env)
    for n, jid in enumerate(("status@broadcast", "123@broadcast", "55@newsletter", "0@s.whatsapp.net")):
        assert ingest(env, account, event(messageId=f"X{n}", chatId=jid, senderId="x@lid")) is None
    assert env.conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0] == 0


def test_repair_keeps_group_conversations(env):
    account = add_account(env)
    ingest(env, account, group_event())
    assert env.core.contacts.repair_lid_conversations(env.conn, account, NOW) == 0
    assert conv(env, account, GROUP) is not None


def test_groups_never_queue_jev_runs_or_automation_runs_but_direct_chats_do(env):
    core = env.core
    account = add_account(env)
    cfg = core.settings.get_settings(env.conn).model_dump()
    cfg["jev"].update(
        {
            "enabled": True,
            "exits": [{"id": "person", "label": "Person", "description": "wants a person", "min_score": 0.7}],
        }
    )
    env.conn.execute(
        "INSERT OR REPLACE INTO settings (key, value, updated_at) VALUES ('global', ?, ?)", (json.dumps(cfg), NOW)
    )
    core.jev.set_api_key(env.conn, "k-test", NOW)
    res = env.client.post(f"{PREFIX}/automations", json={"name": "all", "action": {"type": "escalate"}})
    assert res.status_code == 200, res.text

    ingest(env, account, group_event())
    assert env.conn.execute("SELECT COUNT(*) FROM jev_runs").fetchone()[0] == 0
    assert env.conn.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0] == 0

    ingest(env, account, event(messageId="D1"))
    assert env.conn.execute("SELECT COUNT(*) FROM jev_runs").fetchone()[0] == 1
    assert env.conn.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0] >= 1


# --- Lists ------------------------------------------------------------------------------------


def test_new_conversation_takes_the_numbers_default_list_for_contacts_or_groups(env):
    account = add_account(env, new_contact_list="work", new_group_list="personal")
    ingest(env, account, event())
    ingest(env, account, group_event())
    assert list_of(env, CONTACT_JID) == "work" and list_of(env, GROUP) == "personal"
    assert get_json(env, f"/conversations/{conv(env, account, GROUP)['id']}")["conversation"]["list"] == "personal"
    assert payloads(env, "conversation.created")[1]["list"] == "personal"


def test_a_second_number_reuses_the_list_the_contact_already_has(env):
    first = add_account(env, "A", new_contact_list="work")
    second = add_account(env, "B", wa_jid="393990000001@s.whatsapp.net", new_contact_list="admin")
    ingest(env, first, event())
    ingest(env, second, event(botIds=["393990000001@s.whatsapp.net"]))
    assert list_of(env, CONTACT_JID) == "work"
    detail = get_json(env, f"/conversations/{conv(env, second, CONTACT_JID)['id']}")["conversation"]
    assert (detail["list"], detail["list_scope"]) == ("work", "contact")


def test_set_list_for_the_contact_applies_on_every_number_and_number_scope_overrides_one(env):
    first = add_account(env, "A")
    second = add_account(env, "B", wa_jid="393990000001@s.whatsapp.net")
    ingest(env, first, event())
    ingest(env, second, event(botIds=["393990000001@s.whatsapp.net"]))
    a, b = conv(env, first, CONTACT_JID)["id"], conv(env, second, CONTACT_JID)["id"]

    res = env.client.put(f"{PREFIX}/conversations/{a}/list", json={"list": "work"})
    assert res.status_code == 200 and res.json()["conversation"]["list"] == "work"
    assert get_json(env, f"/conversations/{b}")["conversation"]["list"] == "work"

    res = env.client.put(f"{PREFIX}/conversations/{b}/list", json={"list": "personal", "scope": "number"})
    detail = res.json()["conversation"]
    assert (detail["list"], detail["list_scope"]) == ("personal", "number")
    assert get_json(env, f"/conversations/{a}")["conversation"]["list"] == "work"

    # a contact-wide change clears this conversation's override and reaches the other number
    env.client.put(f"{PREFIX}/conversations/{b}/list", json={"list": "admin"})
    for conv_id in (a, b):
        detail = get_json(env, f"/conversations/{conv_id}")["conversation"]
        assert (detail["list"], detail["list_scope"]) == ("admin", "contact")

    # null clears the override only with number scope
    env.client.put(f"{PREFIX}/conversations/{b}/list", json={"list": "ignored", "scope": "number"})
    env.client.put(f"{PREFIX}/conversations/{b}/list", json={"list": None, "scope": "number"})
    assert get_json(env, f"/conversations/{b}")["conversation"]["list"] == "admin"
    assert env.client.put(f"{PREFIX}/conversations/{b}/list", json={"list": None}).status_code == 400
    assert env.client.put(f"{PREFIX}/conversations/{b}/list", json={"list": "vip"}).status_code == 400
    assert env.client.put(f"{PREFIX}/conversations/999/list", json={"list": "work"}).status_code == 404
    updates = [p for p in payloads(env, "conversation.updated") if p["fields"] == ["list"]]
    assert updates and all(p["actor"] == "user" for p in updates)


def test_list_filters_hide_ignored_by_default_and_select_lists_and_types(env):
    account = add_account(env)
    ingest(env, account, event(messageId="D1", chatId=CONTACT_JID, senderId=CONTACT_JID))
    other = "393330002222@s.whatsapp.net"
    ingest(env, account, event(messageId="D2", chatId=other, senderId=other))
    ingest(env, account, group_event())
    direct_ignored, direct_work = conv(env, account, CONTACT_JID)["id"], conv(env, account, other)["id"]
    group_id = conv(env, account, GROUP)["id"]
    env.core.lists.set_list(env.conn, direct_ignored, "ignored", "contact", now=NOW)
    env.core.lists.set_list(env.conn, direct_work, "work", "contact", now=NOW)

    def ids(path="/conversations", **params):
        body = get_json(env, path, **params)
        if path == "/board":
            return sorted(c["id"] for col in body["columns"] for c in col["cards"])
        return sorted(c["id"] for c in body["conversations"])

    assert ids() == sorted([direct_work, group_id])
    assert ids(list="all") == sorted([direct_ignored, direct_work, group_id])
    assert ids(list="ignored") == [direct_ignored]
    assert ids(list="work") == [direct_work]
    assert ids(list="unclassified") == [group_id]
    assert ids(type="group") == [group_id]
    assert ids(type="direct") == [direct_work]
    assert ids(type="direct", list="all") == sorted([direct_ignored, direct_work])
    assert ids("/board") == sorted([direct_work, group_id])
    assert ids("/board", list="all", type="group") == [group_id]
    counts = get_json(env, "/board")["counts"]
    assert counts["new"] == 2
    total = get_json(env, "/conversations")["total"]
    assert total == 2 and get_json(env, "/conversations", list="all")["total"] == 3
    assert env.client.get(f"{PREFIX}/conversations", params={"list": "vip"}).status_code == 400
    assert env.client.get(f"{PREFIX}/conversations", params={"type": "pairs"}).status_code == 400
    # a search still finds the group
    found = get_json(env, "/search", q="hello")["results"]
    assert group_id in {r["conversation"]["id"] for r in found}
    card = next(r["conversation"] for r in found if r["conversation"]["id"] == group_id)
    assert card["is_group"] is True and card["list"] == "unclassified"


def test_lists_route_returns_the_five_lists_in_order(env):
    assert get_json(env, "/lists") == [
        {"id": "admin", "label": "Admin"},
        {"id": "work", "label": "Work"},
        {"id": "personal", "label": "Personal"},
        {"id": "unclassified", "label": "To classify"},
        {"id": "ignored", "label": "Ignored"},
    ]


def test_patch_account_validates_the_new_chat_lists(env):
    account = add_account(env)
    res = env.client.patch(f"{PREFIX}/accounts/{account}", json={"new_contact_list": "work", "new_group_list": "ignored"})
    assert res.status_code == 200
    assert (res.json()["new_contact_list"], res.json()["new_group_list"]) == ("work", "ignored")
    listed = get_json(env, "/accounts")["accounts"][0]
    assert (listed["new_contact_list"], listed["new_group_list"]) == ("work", "ignored")
    assert env.client.patch(f"{PREFIX}/accounts/{account}", json={"new_contact_list": "vip"}).status_code == 400
    assert env.client.patch(f"{PREFIX}/accounts/{account}", json={"new_group_list": None}).status_code == 400
    assert get_json(env, "/accounts")["accounts"][0]["new_contact_list"] == "work"


def test_lid_rename_moves_the_list_to_the_phone_jid_and_merge_keeps_the_phone_row(env):
    account = add_account(env)
    session = env.core.db.data_dir() / "sessions" / str(account)
    (session / "lid-mapping-555_reverse.json").write_text('"393330001111"', encoding="utf-8")
    (session / "lid-mapping-666_reverse.json").write_text('"393330002222"', encoding="utf-8")
    with env.core.db.write_txn(env.conn):
        for jid in ("555@lid", "666@lid"):
            env.conn.execute(
                "INSERT INTO conversations (account_id, chat_jid, state, created_at) VALUES (?,?,'new',?)",
                (account, jid, NOW),
            )
        env.conn.execute(
            "INSERT INTO conversations (account_id, chat_jid, state, created_at) VALUES (?,?, 'new', ?)",
            (account, "393330002222@s.whatsapp.net", NOW),
        )
        env.conn.executemany(
            "INSERT INTO contact_lists (jid, list, updated_at) VALUES (?,?,?)",
            [("555@lid", "work", NOW), ("666@lid", "admin", NOW), ("393330002222@s.whatsapp.net", "personal", NOW)],
        )
    assert env.core.contacts.repair_lid_conversations(env.conn, account, NOW) == 2
    assert list_of(env, "393330001111@s.whatsapp.net") == "work"
    assert list_of(env, "555@lid") is None
    # merged into the existing phone conversation: its own list wins
    assert list_of(env, "393330002222@s.whatsapp.net") == "personal"
    assert list_of(env, "666@lid") is None


# --- Silence ----------------------------------------------------------------------------------


def test_silence_hours_forever_and_unsilence(env):
    account = add_account(env)
    ingest(env, account, event())
    conv_id = conv(env, account, CONTACT_JID)["id"]

    res = env.client.post(f"{PREFIX}/conversations/{conv_id}/silence", json={"hours": 8})
    detail = res.json()["conversation"]
    assert res.status_code == 200 and detail["silenced"] is True
    assert abs(detail["silenced_until"] - (int(time.time()) + 8 * 3600)) < 5

    forever = env.client.post(f"{PREFIX}/conversations/{conv_id}/silence").json()["conversation"]
    assert forever["silenced_until"] == env.core.lists.SILENCE_FOREVER and forever["silenced"] is True
    forever_json = env.client.post(f"{PREFIX}/conversations/{conv_id}/silence", json={"hours": None}).json()
    assert forever_json["conversation"]["silenced_until"] == env.core.lists.SILENCE_FOREVER

    for bad in (0, 8761, -1):
        assert env.client.post(f"{PREFIX}/conversations/{conv_id}/silence", json={"hours": bad}).status_code == 400
    assert env.client.post(f"{PREFIX}/conversations/999/silence").status_code == 404

    res = env.client.post(f"{PREFIX}/conversations/{conv_id}/unsilence")
    assert res.json()["conversation"]["silenced"] is False and res.json()["conversation"]["silenced_until"] is None
    fields = [p["fields"] for p in payloads(env, "conversation.updated")]
    assert ["silenced_until"] in fields


def test_event_payloads_carry_the_silenced_flag_and_the_effective_list(env):
    account = add_account(env)
    ingest(env, account, event(messageId="A"))
    conv_id = conv(env, account, CONTACT_JID)["id"]
    env.core.lists.silence(env.conn, conv_id, None, now=NOW)
    env.core.lists.set_list(env.conn, conv_id, "ignored", "contact", now=NOW)
    ingest(env, account, event(messageId="B"))
    first, second = payloads(env, "message.in")
    assert (first["silenced"], first["list"], first["is_group"]) == (False, "unclassified", False)
    assert (second["silenced"], second["list"]) == (True, "ignored")
    # an expired silence is not silenced
    env.core.lists.silence(env.conn, conv_id, 1, now=NOW - 7200)
    ingest(env, account, event(messageId="C"))
    assert payloads(env, "message.in")[2]["silenced"] is False


# --- Group metadata ---------------------------------------------------------------------------


def test_apply_metadata_sets_subject_participants_and_names(env):
    account = add_account(env)
    ingest(env, account, group_event(messageId="G1"))
    ingest(env, account, event(messageId="D1", chatId=BOB, senderId=BOB, senderName="Bobby"))
    env.conn.execute("UPDATE conversations SET contact_name = 'Bob (saved)' WHERE chat_jid = ?", (BOB,))
    conv_id = conv(env, account, GROUP)["id"]
    session = env.core.db.data_dir() / "sessions" / str(account)
    (session / "lid-mapping-888_reverse.json").write_text('"393201110003"', encoding="utf-8")
    data = {
        "name": "Team Ops",
        "isGroup": True,
        "participants": ["x"],
        "participantDetails": [
            {"id": "111@lid", "phone": ALICE, "lid": "111@lid", "admin": "superadmin"},
            {"id": BOB, "phone": BOB, "lid": None, "admin": None},
            {"id": "888@lid", "phone": None, "lid": "888@lid", "admin": "admin"},
            {"id": "999@lid", "phone": None, "lid": "999@lid", "admin": None},
        ],
    }
    before = env.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'conversation.updated'").fetchone()[0]
    assert env.core.groups.apply_metadata(env.conn, conv_id, data, NOW + 5) is True
    row = conv(env, account, GROUP)
    assert (row["contact_name"], row["group_refreshed_at"]) == ("Team Ops", NOW + 5)
    people = json.loads(row["participants"])
    assert people == [
        {"jid": ALICE, "phone": "393201110001", "admin": "superadmin"},
        {"jid": BOB, "phone": "393201110002", "admin": False},
        {"jid": "393201110003@s.whatsapp.net", "phone": "393201110003", "admin": True},
        {"jid": "999@lid", "phone": None, "admin": False},
    ]
    event_rows = payloads(env, "conversation.updated")[before:]
    assert event_rows == [{"fields": ["participants", "contact_name"]}]

    # unchanged answer: the refresh time moves, nothing is announced
    assert env.core.groups.apply_metadata(env.conn, conv_id, data, NOW + 10) is False
    assert conv(env, account, GROUP)["group_refreshed_at"] == NOW + 10
    assert len(payloads(env, "conversation.updated")) == before + 1

    detail = get_json(env, f"/conversations/{conv_id}")["conversation"]
    names = {p["jid"]: p["name"] for p in detail["participants"]}
    assert names[ALICE] == "Alice Martin"  # latest sender name seen in this group
    assert names[BOB] == "Bob (saved)"  # name of the direct chat with that phone
    assert names["999@lid"] is None
    assert detail["is_group"] is True and detail["contact_name"] == "Team Ops"


def test_apply_metadata_ignores_a_failed_lookup_and_non_groups(env):
    account = add_account(env)
    ingest(env, account, group_event())
    ingest(env, account, event())
    group_id, direct_id = conv(env, account, GROUP)["id"], conv(env, account, CONTACT_JID)["id"]
    broken = {"name": GROUP.split("@")[0], "isGroup": True, "participants": []}
    assert env.core.groups.apply_metadata(env.conn, group_id, broken, NOW) is False
    assert conv(env, account, GROUP)["contact_name"] is None
    assert env.core.groups.apply_metadata(env.conn, direct_id, {"name": "X", "participantDetails": []}, NOW) is False
    assert conv(env, account, CONTACT_JID)["group_refreshed_at"] is None


def test_mark_refresh_failed_defers_the_group(env):
    account = add_account(env)
    ingest(env, account, group_event())
    conv_id = conv(env, account, GROUP)["id"]
    env.core.groups.mark_refresh_failed(env.conn, conv_id, NOW + 3)
    assert conv(env, account, GROUP)["group_refreshed_at"] == NOW + 3


# --- Outbound ---------------------------------------------------------------------------------


def test_reply_to_a_group_sends_to_the_group_jid(env):
    account = add_account(env)
    ingest(env, account, group_event())
    conv_id = conv(env, account, GROUP)["id"]
    env.replies["/send"] = (200, {"success": True, "messageId": "OUT1"})
    res = env.client.post(f"{PREFIX}/conversations/{conv_id}/reply", json={"text": "Thanks all"})
    assert res.status_code == 200, res.text
    (call,) = [c for c in env.calls if c[2] == "/send"]
    assert call[3] == {"chatId": GROUP, "message": "Thanks all"}


def test_new_chat_still_refuses_groups(env):
    account = add_account(env)
    env.replies["/check"] = (200, {"results": [{"phone": "393331234567", "exists": True, "jid": GROUP}]})
    res = env.client.post(f"{PREFIX}/conversations", json={"phone": "+393331234567", "text": "Hi", "mode": "draft"})
    assert res.status_code == 502 and "no WhatsApp address" in res.json()["detail"]
    assert conv(env, account, GROUP) is None


def test_read_receipts_for_group_messages_include_the_participant(env):
    account = add_account(env)
    ingest(env, account, group_event(messageId="GM1", participant=ALICE))
    ingest(env, account, event(messageId="DM1"))
    group_id, direct_id = conv(env, account, GROUP)["id"], conv(env, account, CONTACT_JID)["id"]
    env.replies["/read"] = (200, {"success": True, "marked": 1})
    assert env.client.post(f"{PREFIX}/conversations/{group_id}/read").status_code == 200
    assert env.client.post(f"{PREFIX}/conversations/{direct_id}/read").status_code == 200
    group_call, direct_call = [c for c in env.calls if c[2] == "/read"]
    assert group_call[3]["keys"] == [{"remoteJid": GROUP, "id": "GM1", "fromMe": False, "participant": ALICE}]
    assert direct_call[3]["keys"] == [{"remoteJid": CONTACT_JID, "id": "DM1", "fromMe": False}]


# --- Version and demo -------------------------------------------------------------------------


def test_api_and_schema_versions(env):
    assert env.core.service.API_VERSION == 10
    assert env.core.db.SCHEMA_VERSION == 6
    assert env.client.get(f"{PREFIX}/service").json()["api_version"] == 10


def test_demo_seed_has_lists_groups_and_one_ignored_chat(env):
    seed = _load("hwc_seed_demo_groups", SEED_FILE)
    seed.seed(env.conn, 60, NOW)
    allc = get_json(env, "/conversations", list="all", limit=200)
    assert allc["total"] == 60
    groups = get_json(env, "/conversations", list="all", type="group")["conversations"]
    assert len(groups) == 2 and all(g["is_group"] for g in groups)
    assert {g["list"] for g in groups} == {"work", "personal"}
    detail = get_json(env, f"/conversations/{groups[0]['id']}")["conversation"]
    assert len(detail["participants"]) == 5 and detail["participants"][0]["admin"] is True
    thread = get_json(env, f"/conversations/{groups[0]['id']}/messages")["messages"]
    assert any(m["sender_name"] for m in thread if m["direction"] == "in")
    assert get_json(env, "/conversations", list="ignored")["total"] == 1
    assert get_json(env, "/conversations", list="all")["total"] == 60
    assert get_json(env, "/conversations")["total"] == 59
    seed.remove_demo(env.conn)
    assert env.conn.execute("SELECT COUNT(*) FROM contact_lists").fetchone()[0] == 0


# --- CLI --------------------------------------------------------------------------------------


@pytest.fixture
def cli(env):
    mod = _load("hwc_wa_cli_groups_test", CLI_FILE)
    return SimpleNamespace(main=mod.main)


def wa(cli, capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_list_shows_lists_groups_and_filters(env, cli, capsys):
    account = add_account(env)
    ingest(env, account, event(messageId="D1"))
    ingest(env, account, group_event())
    direct_id, group_id = conv(env, account, CONTACT_JID)["id"], conv(env, account, GROUP)["id"]

    code, out, _ = wa(cli, capsys, "list")
    assert code == 0 and "LIST" in out and "[group]" in out and "unclassified" in out

    code, out, _ = wa(cli, capsys, "set-list", str(direct_id), "ignored")
    assert code == 0 and "list=ignored" in out
    code, out, _ = wa(cli, capsys, "list", "--json")
    assert [c["id"] for c in json.loads(out)["conversations"]] == [group_id]
    code, out, _ = wa(cli, capsys, "list", "--list", "all", "--direct", "--json")
    assert [c["id"] for c in json.loads(out)["conversations"]] == [direct_id]
    code, out, _ = wa(cli, capsys, "list", "--groups", "--json")
    assert [c["id"] for c in json.loads(out)["conversations"]] == [group_id]
    with pytest.raises(SystemExit):  # argparse: --groups and --direct exclude each other
        wa(cli, capsys, "list", "--groups", "--direct")


def test_cli_set_list_this_number_silence_and_unsilence(env, cli, capsys):
    account = add_account(env)
    ingest(env, account, group_event())
    group_id = conv(env, account, GROUP)["id"]

    code, out, _ = wa(cli, capsys, "set-list", str(group_id), "work", "--this-number")
    assert code == 0 and "only this number" in out
    row = conv(env, account, GROUP)
    assert row["list_override"] == "work" and list_of(env, GROUP) == "unclassified"

    code, _, err = wa(cli, capsys, "set-list", str(group_id), "vip")
    assert code == 1 and "list must be one of" in err

    code, out, _ = wa(cli, capsys, "silence", str(group_id))
    assert code == 0 and "forever" in out
    code, out, _ = wa(cli, capsys, "silence", str(group_id), "--hours", "2")
    assert code == 0 and "until" in out
    assert wa(cli, capsys, "silence", str(group_id), "--hours", "0")[0] == 1
    code, out, _ = wa(cli, capsys, "unsilence", str(group_id))
    assert code == 0 and "not silenced" in out
    assert conv(env, account, GROUP)["silenced_until"] is None


def test_cli_show_names_the_sender_of_group_messages(env, cli, capsys):
    account = add_account(env)
    ingest(env, account, group_event(messageId="G1", body="who is coming?"))
    ingest(env, account, group_event(messageId="G2", senderId=BOB, participant=BOB, senderName="Bob", body="me"))
    group_id = conv(env, account, GROUP)["id"]
    env.core.groups.apply_metadata(
        env.conn, group_id, {"name": "Team Ops", "participantDetails": [{"id": ALICE, "phone": ALICE, "admin": "admin"}]}, NOW
    )
    code, out, _ = wa(cli, capsys, "show", str(group_id))
    assert code == 0
    assert "Group #" in out and "Team Ops" in out and "Participants: 1" in out and "List: To classify" in out
    assert "from Alice Martin (+393201110001)" in out
    assert "from Bob (+393201110002)" in out
    code, out, _ = wa(cli, capsys, "show", str(group_id), "--json")
    messages = json.loads(out)["messages"]
    assert [m["sender_name"] for m in messages] == ["Alice Martin", "Bob"]
