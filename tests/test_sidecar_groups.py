"""Sidecar group support: bridge environment and the group metadata refresh.

The backend module ``core.groups`` and the bridge HTTP client are faked; the SQLite DB holds only the
columns the sidecar's selection reads, so these tests do not depend on the full schema.
"""

from __future__ import annotations

import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CHANNEL_FILE = REPO_ROOT / "plugin" / "sidecar" / "wa_channel.py"
NOW = 1_800_000_000
HOURS = 3600


@pytest.fixture
def channel(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))
    monkeypatch.setenv("WA_ARCHIVE_DB", str(tmp_path / "wa_board.db"))
    name = "hwc_wa_channel_groups_test"
    spec = importlib.util.spec_from_file_location(name, CHANNEL_FILE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE accounts (id INTEGER PRIMARY KEY, kind TEXT, desired TEXT, port INTEGER);
        CREATE TABLE account_status (account_id INTEGER PRIMARY KEY, state TEXT);
        CREATE TABLE conversations (
          id INTEGER PRIMARY KEY, account_id INTEGER, chat_jid TEXT, is_group INTEGER, group_refreshed_at INTEGER);
        INSERT INTO accounts VALUES (1, 'whatsapp', 'running', 3017), (2, 'whatsapp', 'running', 3018),
                                    (3, 'demo', 'running', 3019), (4, 'whatsapp', 'stopped', 3020);
        INSERT INTO account_status VALUES (1, 'connected'), (2, 'disconnected'), (3, 'connected'), (4, 'connected');
        """
    )
    return conn


def add_conversation(db, cid, account_id, jid, is_group, refreshed_at):
    db.execute(
        "INSERT INTO conversations VALUES (?, ?, ?, ?, ?)", (cid, account_id, jid, is_group, refreshed_at)
    )


class FakeGroups:
    def __init__(self):
        self.applied: list[tuple] = []
        self.failed: list[tuple] = []
        self.apply_error: Exception | None = None

    def apply_metadata(self, conn, conversation_id, data, now):
        if self.apply_error:
            raise self.apply_error
        self.applied.append((conversation_id, data, now))

    def mark_refresh_failed(self, conn, conversation_id, now):
        self.failed.append((conversation_id, now))


@pytest.fixture
def groups(channel, monkeypatch):
    fake = FakeGroups()
    monkeypatch.setattr(channel.core, "groups", fake, raising=False)
    return fake


@pytest.fixture
def bridge(channel, monkeypatch):
    calls: list[tuple] = []
    replies: dict = {}

    def fake(port, method, path, payload=None, *, timeout):
        calls.append((port, method, path, timeout))
        reply = replies.get(path)
        if reply is None:
            raise channel.core.bridge.BridgeUnavailable("down")
        return reply

    monkeypatch.setattr(channel.core.bridge, "bridge_request", fake)
    return calls, replies


@pytest.fixture
def supervisor(channel):
    return object.__new__(channel.Supervisor)


def details(subject="Team"):
    return {
        "name": subject,
        "isGroup": True,
        "participants": ["1@s.whatsapp.net"],
        "participantDetails": [{"id": "1@s.whatsapp.net", "phone": "1@s.whatsapp.net", "lid": None, "admin": None}],
    }


def test_bridge_env_opens_group_policy_and_forwards_owner_messages(channel):
    env = channel.bridge_env({"id": 1, "port": 3017, "session_dir": "sessions/1"})
    assert env["WHATSAPP_GROUP_POLICY"] == "open"
    assert env["WHATSAPP_ALLOWED_USERS"] == "*"
    assert env["WHATSAPP_FORWARD_OWNER_MESSAGES"] == "true"


def test_stale_groups_picks_only_stale_groups_of_connected_numbers_oldest_first_max_three(channel, db):
    add_conversation(db, 1, 1, "g1@g.us", 1, NOW - 7 * HOURS)  # stale
    add_conversation(db, 2, 1, "g2@g.us", 1, None)  # never read
    add_conversation(db, 3, 1, "g3@g.us", 1, NOW - 1 * HOURS)  # fresh
    add_conversation(db, 4, 1, "dm@s.whatsapp.net", 0, None)  # not a group
    add_conversation(db, 5, 2, "g5@g.us", 1, None)  # number not connected
    add_conversation(db, 6, 3, "g6@g.us", 1, None)  # demo number
    add_conversation(db, 7, 4, "g7@g.us", 1, None)  # stopped number
    add_conversation(db, 8, 1, "g8@g.us", 1, NOW - 30 * HOURS)  # stalest
    add_conversation(db, 9, 1, "g9@g.us", 1, NOW - 8 * HOURS)
    add_conversation(db, 10, 1, "g10@g.us", 1, NOW - 6 * HOURS)  # exactly at the interval: not older, kept out
    rows = channel.stale_groups(db, NOW)
    # never read first, then oldest first, capped at 3
    assert [r["id"] for r in rows] == [2, 8, 9]
    assert {r["port"] for r in rows} == {3017}
    # the one at the cap boundary is picked once the others were refreshed
    for cid in (2, 8, 9):
        db.execute("UPDATE conversations SET group_refreshed_at = ? WHERE id = ?", (NOW, cid))
    assert [r["id"] for r in channel.stale_groups(db, NOW)] == [1]


def test_refresh_reads_each_stale_group_from_its_bridge_and_applies_the_metadata(
    channel, db, groups, bridge, supervisor
):
    calls, replies = bridge
    add_conversation(db, 1, 1, "120363@g.us", 1, None)
    add_conversation(db, 2, 1, "130363@g.us", 1, NOW - 9 * HOURS)
    data = details()
    replies["/chat/120363%40g.us"] = (200, data)
    replies["/chat/130363%40g.us"] = (200, details("Other"))
    supervisor._refresh_groups(db, NOW)
    assert calls == [
        (3017, "GET", "/chat/120363%40g.us", channel.GROUP_INFO_TIMEOUT),
        (3017, "GET", "/chat/130363%40g.us", channel.GROUP_INFO_TIMEOUT),
    ]
    assert channel.GROUP_INFO_TIMEOUT == 10.0
    assert groups.applied == [(1, data, NOW), (2, details("Other"), NOW)]
    assert groups.failed == []


def test_refresh_does_not_touch_groups_that_are_not_stale(channel, db, groups, bridge, supervisor):
    calls, _ = bridge
    add_conversation(db, 1, 1, "120363@g.us", 1, NOW - 2 * HOURS)
    add_conversation(db, 2, 2, "130363@g.us", 1, None)  # number not connected
    supervisor._refresh_groups(db, NOW)
    assert calls == [] and groups.applied == [] and groups.failed == []


def test_refresh_calls_the_bridge_at_most_three_times_per_tick(channel, db, groups, bridge, supervisor):
    calls, replies = bridge
    for cid in range(1, 6):
        add_conversation(db, cid, 1, f"g{cid}@g.us", 1, None)
        replies[f"/chat/g{cid}%40g.us"] = (200, details())
    supervisor._refresh_groups(db, NOW)
    assert len(calls) == 3 and [a[0] for a in groups.applied] == [1, 2, 3]


def test_refresh_marks_the_group_failed_when_the_bridge_is_unreachable(channel, db, groups, bridge, supervisor):
    add_conversation(db, 1, 1, "120363@g.us", 1, None)  # no reply registered: BridgeUnavailable
    supervisor._refresh_groups(db, NOW)
    assert groups.applied == [] and groups.failed == [(1, NOW)]


def test_refresh_marks_the_group_failed_when_the_bridge_has_no_group_details(
    channel, db, groups, bridge, supervisor
):
    _, replies = bridge
    add_conversation(db, 1, 1, "a@g.us", 1, None)
    add_conversation(db, 2, 1, "b@g.us", 1, None)
    replies["/chat/a%40g.us"] = (200, {"name": "a", "isGroup": True, "participants": []})  # metadata call refused
    replies["/chat/b%40g.us"] = (500, {"error": "boom"})
    supervisor._refresh_groups(db, NOW)
    assert groups.applied == [] and groups.failed == [(1, NOW), (2, NOW)]


def test_refresh_marks_the_group_failed_when_applying_the_metadata_raises(
    channel, db, groups, bridge, supervisor
):
    _, replies = bridge
    add_conversation(db, 1, 1, "a@g.us", 1, None)
    add_conversation(db, 2, 1, "b@g.us", 1, None)
    replies["/chat/a%40g.us"] = (200, details())
    replies["/chat/b%40g.us"] = (200, details())
    groups.apply_error = ValueError("bad metadata")
    supervisor._refresh_groups(db, NOW)  # one broken group never stops the others
    assert groups.applied == [] and groups.failed == [(1, NOW), (2, NOW)]
