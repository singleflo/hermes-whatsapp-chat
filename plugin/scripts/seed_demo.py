"""Seed (or remove) demo conversations for hermes-whatsapp-chat.

    seed_demo.py --reset      # replace demo data (default 400 chats)
    seed_demo.py --remove     # delete demo data only

Run with the plugin backend's Python (<data>/bin/hwc-python).

Demo chats live in a ``kind='demo'`` account labelled "Demo" (JIDs use the non-WhatsApp domain
``@demo.invalid``): replies to them are rejected and ``--remove`` deletes only that account's data.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import random
import sqlite3
import sys
import time
from pathlib import Path

PLUGIN_FILE = Path(__file__).resolve().parents[1] / "dashboard" / "plugin_api.py"
DEMO_SUFFIX = "@demo.invalid"

ACTIVE_COUNTS = {"new": 8, "in_progress": 9, "waiting": 7, "muted": 4}
AGE_RANGES = {  # last message age, seconds
    "new": (5 * 60, 30 * 3600),
    "in_progress": (10 * 60, 48 * 3600),
    "waiting": (3600, 96 * 3600),
    "muted": (24 * 3600, 120 * 3600),
    "closed": (2 * 86400, 90 * 86400),
}
NAMES = [
    "Alice Martin", "Lucas Silva", "Sofia Rossi", "Noah Schmidt", "Emma Johnson", "Liam Dubois",
    "Olivia Garcia", "Mateo Fernandez", "Mia Novak", "Ethan Brown", "Chloe Laurent", "Leo Bianchi",
    "Zoe Kowalski", "Hugo Moreau", "Ava Wilson", "Marco Ricci", "Lena Fischer", "Oscar Lindgren",
    "Nina Petrova", "Jack Taylor", "Elena Costa", "Adam Nowak", "Clara Weber", "Ivan Horvat",
    "Sara Almeida", "Tom Evans", "Julia Santos", "Paul Mercier", "Anna Kovacs", "Daniel Ferreira",
]
INBOUND = [
    "Hi, I'd like some information about my order.",
    "Can you tell me when it will be delivered?",
    "I have a problem with the invoice I received.",
    "Is it possible to get a quote for ten units?",
    "Thanks, but I still haven't heard back from anyone.",
    "Could you send me the documents again?",
    "I want to file a complaint about the last delivery.",
    "Good morning, are you open on Saturday?",
]
OUTBOUND = [
    "Hello! Let me check that for you right away.",
    "Thanks for your patience, we are looking into it.",
    "Your order has been shipped and should arrive soon.",
    "I've sent the corrected invoice to your email.",
    "Here is the quote you asked for. Let me know if it works.",
    "We are open from 9 to 17 on Saturdays.",
    "Sorry for the inconvenience, we will fix this today.",
    "Documents sent, please confirm that you received them.",
]
TAGS = ["case", "order", "complaint", "info", "quote"]


def _load_api():
    spec = importlib.util.spec_from_file_location("hwc_plugin_api", PLUGIN_FILE)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _demo_account_id(conn: sqlite3.Connection, now: int) -> int:
    row = conn.execute("SELECT id FROM accounts WHERE kind = 'demo' ORDER BY id LIMIT 1").fetchone()
    if row is not None:
        return row[0]
    cur = conn.execute(
        "INSERT INTO accounts (kind, label, color, created_at, updated_at) VALUES ('demo', 'Demo', 'gray', ?, ?)",
        (now, now),
    )
    return cur.lastrowid or 0


def seed(conn: sqlite3.Connection, count: int, now: int) -> dict[str, int]:
    active_total = sum(ACTIVE_COUNTS.values())
    if count < active_total:
        raise ValueError(f"count must be at least {active_total}")
    rng = random.Random(42)
    states = [s for s, n in ACTIVE_COUNTS.items() for _ in range(n)]
    states += ["closed"] * (count - active_total)
    counts = {s: 0 for s in (*ACTIVE_COUNTS, "closed")}
    in_progress_seen = 0

    conn.execute("BEGIN IMMEDIATE")
    try:
        account_id = _demo_account_id(conn, now)
        for state in states:
            index = counts[state]
            counts[state] += 1
            phone = "39" + str(rng.randint(3200000000, 3999999999))
            jid = f"{phone}{DEMO_SUFFIX}"
            low, high = AGE_RANGES[state]
            last_at = now - rng.randint(low, high)
            muted_until = now + rng.randint(2 * 3600, 72 * 3600) if state == "muted" else None

            last_direction = "out" if state in ("waiting", "closed") else "in"
            n_msgs = rng.randint(2, 6)
            directions = [last_direction if (n_msgs - 1 - i) % 2 == 0 else ("in" if last_direction == "out" else "out")
                          for i in range(n_msgs)]
            ts = last_at
            msgs = []
            for direction in reversed(directions):
                msgs.append((direction, rng.choice(INBOUND if direction == "in" else OUTBOUND), ts))
                ts -= rng.randint(2 * 60, 30 * 60)
            msgs.reverse()
            last_inbound_at = max((t for d, _, t in msgs if d == "in"), default=None)

            unread = {"new": rng.randint(1, 3), "in_progress": rng.randint(0, 2)}.get(state, 0)
            priority = 2 if (state == "new" and index == 0) else 1 if (
                (state == "in_progress" and index < 2) or (state == "waiting" and index == 0)) else 0
            agent_active = 0 if (state == "in_progress" and index < 3) else 1
            tags = rng.sample(TAGS, rng.randint(0, 2))
            if state == "in_progress":
                in_progress_seen += 1
                if in_progress_seen == 4:
                    tags = ["urgent"]

            conv_id = conn.execute(
                "INSERT INTO conversations (account_id, chat_jid, contact_name, phone, state, priority, muted_until,"
                " last_message_at, last_inbound_at, unread_count, agent_active, tags, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (account_id, jid, rng.choice(NAMES), phone, state, priority, muted_until, last_at, last_inbound_at,
                 unread, agent_active, json.dumps(tags), msgs[0][2], last_at),
            ).lastrowid
            conn.executemany(
                "INSERT INTO messages (conversation_id, account_id, wa_id, direction, author, body, ts, status)"
                " VALUES (?,?,NULL,?,?,?,?,?)",
                [(conv_id, account_id, d, "contact" if d == "in" else "user", b, t,
                  "received" if d == "in" else "sent") for d, b, t in msgs],
            )
            conn.execute(
                "INSERT INTO conversation_state_log (conversation_id, from_state, to_state, actor, reason, at)"
                " VALUES (?,NULL,?,'auto','seed',?)",
                (conv_id, state, last_at),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return counts


def remove_demo(conn: sqlite3.Connection) -> int:
    """Delete every demo account with its conversations, messages, logs, events and runs."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        accounts = "SELECT id FROM accounts WHERE kind = 'demo'"
        convs = f"SELECT id FROM conversations WHERE account_id IN ({accounts})"
        for table in ("messages", "conversation_state_log", "automation_runs", "events"):
            conn.execute(f"DELETE FROM {table} WHERE conversation_id IN ({convs})")
        removed = conn.execute(f"DELETE FROM conversations WHERE account_id IN ({accounts})").rowcount
        conn.execute(f"DELETE FROM account_status WHERE account_id IN ({accounts})")
        conn.execute("DELETE FROM accounts WHERE kind = 'demo'")
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", help="SQLite path (default: the plugin's own DB)")
    parser.add_argument("--count", type=int, default=400)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--reset", action="store_true", help="remove existing demo data first")
    group.add_argument("--remove", action="store_true", help="remove demo data and exit")
    args = parser.parse_args()

    if args.db:
        os.environ["WA_ARCHIVE_DB"] = args.db
    db = _load_api().core.db
    path = db.db_path()
    conn = db.connect()
    try:
        if args.remove:
            print(f"removed {remove_demo(conn)} demo conversations from {path}")
            return 0
        has_demo = conn.execute("SELECT 1 FROM accounts WHERE kind = 'demo' LIMIT 1").fetchone()
        if has_demo and not args.reset:
            print(f"demo data already present in {path} (use --reset or --remove)")
            return 1
        if args.reset:
            remove_demo(conn)
        counts = seed(conn, args.count, int(time.time()))
    finally:
        conn.close()
    print(path)
    print(", ".join(f"{state} {n}" for state, n in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
