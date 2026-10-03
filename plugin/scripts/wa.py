"""CLI for Hermes (and humans) to operate the WhatsApp chat plugin.

Run with the plugin backend's Python: <data>/bin/wa <command>   (launcher written by the plugin's Install service)

    list [--state S] [--account ID] [--unread] [--limit N] [--json]
    show ID [--limit N] [--json]
    send ID TEXT              send now (only when the user explicitly asked)
    draft ID TEXT             store a draft for the human to review
    state ID STATE [--reason R]
    tag ID +a -b
    takeover ID | handback ID
    search QUERY [--account ID]

Author of send/draft/state changes: agent:$HERMES_PROFILE when set, else cli.
Exit codes: 0 ok, 1 error (message on stderr). `--json` prints machine-readable output.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import Any

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def _load_api():
    spec = importlib.util.spec_from_file_location("hwc_plugin_api", PLUGIN_DIR / "dashboard" / "plugin_api.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


api = _load_api()
core = api.core


def author() -> str:
    profile = os.environ.get("HERMES_PROFILE", "").strip()
    return f"agent:{profile}" if profile else "cli"


def fmt_time(ts: int | None) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "-"


def fmt_age(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def one_line(text: Any, width: int) -> str:
    flat = " ".join(str(text or "").split())
    return flat if len(flat) <= width else flat[: width - 1] + "…"


def table(rows: list[list[str]]) -> str:
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in rows)


def emit(args: argparse.Namespace, data: Any, text: str) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2) if args.json else text)


def summary_text(s: dict) -> str:
    tags = ",".join(s.get("tags") or []) or "-"
    return f"#{s['id']} state={s['state']} agent_active={s['agent_active']} unread={s['unread_count']} tags={tags}"


# --- commands --------------------------------------------------------------------------


def cmd_list(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    res = core.conversations.list_conversations(
        conn,
        account_id=args.account,
        state=args.state,
        unread_only=args.unread,
        limit=args.limit,
        now=int(time.time()),
    )
    cards = res["conversations"]
    if args.json:
        emit(args, res, "")
        return
    if not cards:
        print("no conversations")
        return
    rows = [["ID", "ACCOUNT", "STATE", "UNREAD", "AGE", "CONTACT", "LAST MESSAGE"]]
    for c in cards:
        who = c.get("contact_name") or c.get("phone") or c["chat_jid"]
        arrow = "<" if c.get("last_message_direction") == "in" else ">"
        draft = " [draft]" if c.get("has_draft") else ""
        rows.append(
            [
                str(c["id"]),
                str(c.get("account_label") or c["account_id"]),
                c["state"] + ("*" if c.get("priority") else ""),
                str(c.get("unread_count") or 0),
                fmt_age(c.get("age_seconds")),
                one_line(who, 24),
                f"{arrow} {one_line(c.get('last_message_preview'), 60)}{draft}",
            ]
        )
    print(table(rows))
    print(f"{len(cards)} of {res['total']}")


def render_message(m: dict) -> str:
    media = "".join(f" [media: {x.get('type') or x.get('mime') or 'file'}]" for x in m.get("media") or [])
    status = m["status"]
    head = f"[{fmt_time(m['ts'])}] #{m['id']} {m['author']} ({status})"
    if m.get("error"):
        head += f" error: {m['error']}"
    body = (m.get("body") or "").rstrip()
    indented = "\n".join("    " + line for line in body.splitlines()) if body else "    (no text)"
    return f"{head}{media}\n{indented}"


def cmd_show(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    now = int(time.time())
    detail = core.conversations.get_conversation(conn, args.id, now=now)
    msgs = core.conversations.list_messages(conn, args.id, limit=args.limit)
    if args.json:
        emit(args, {**detail, "messages": msgs["messages"], "has_more": msgs["has_more"]}, "")
        return
    c = detail["conversation"]
    acc = detail.get("account") or {}
    who = c.get("contact_name") or c.get("phone") or c["chat_jid"]
    print(f"Conversation #{c['id']}: {who} ({c['chat_jid']})")
    print(f"Account: {acc.get('label') or c['account_id']} ({acc.get('kind') or '?'})")
    print(
        f"State: {c['state']}  agent_active: {c['agent_active']}  unread: {c['unread_count']}"
        f"  tags: {','.join(c.get('tags') or []) or '-'}"
    )
    print(f"Allowed next states: {', '.join(detail.get('allowed_states') or []) or '-'}")
    print()
    if msgs["has_more"]:
        print(f"... older messages omitted (--limit {args.limit})\n")
    if not msgs["messages"]:
        print("(no messages)")
    for m in msgs["messages"]:
        print(render_message(m))


def cmd_send(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    msg = core.outbound.send_text(conn, args.id, args.text, author=author(), now=int(time.time()))
    emit(args, msg, f"sent #{msg['id']} to conversation {args.id} (status {msg['status']})")


def cmd_draft(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    msg = core.outbound.create_draft(conn, args.id, args.text, author=author(), now=int(time.time()))
    emit(args, msg, f"draft #{msg['id']} saved for conversation {args.id} (a human approves it in the UI)")


def cmd_state(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    s = core.conversations.set_state(conn, args.id, args.state, now=int(time.time()), actor=author(), reason=args.reason)
    emit(args, s, summary_text(s))


def cmd_tag(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    add: list[str] = []
    remove: list[str] = []
    for token in args.tags:
        if token.startswith("-") and len(token) > 1:
            remove.append(token[1:])
        elif token.startswith("+") and len(token) > 1:
            add.append(token[1:])
        elif token and token[0] not in "+-":
            add.append(token)
        else:
            raise ValueError(f"bad tag token {token!r}: use +name to add, -name to remove")
    if not add and not remove:
        raise ValueError("no tags given: wa.py tag ID +name -name")
    s = core.conversations.update_tags(conn, args.id, add=add, remove=remove, now=int(time.time()))
    emit(args, s, summary_text(s))


def cmd_takeover(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    s = core.conversations.takeover(conn, args.id, now=int(time.time()), actor=author())
    emit(args, s, summary_text(s))


def cmd_handback(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    s = core.conversations.handback(conn, args.id, now=int(time.time()), actor=author())
    emit(args, s, summary_text(s))


def cmd_search(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    res = core.conversations.search_messages(conn, args.query, account_id=args.account, now=int(time.time()))
    if args.json:
        emit(args, res, "")
        return
    if not res["results"]:
        print("no matches")
        return
    rows = [["CONV", "ACCOUNT", "WHEN", "AUTHOR", "MESSAGE"]]
    for hit in res["results"]:
        m, c = hit["message"], hit["conversation"]
        rows.append(
            [
                str(c["id"]),
                str(c.get("account_label") or c["account_id"]),
                fmt_time(m["ts"]),
                str(m["author"]),
                one_line(m.get("body"), 80),
            ]
        )
    print(table(rows))


# --- parser ----------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wa.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list", parents=[common], help="list conversations, newest activity first")
    p.add_argument("--state", help="new|in_progress|waiting|muted|closed")
    p.add_argument("--account", type=int, help="account id")
    p.add_argument("--unread", action="store_true", help="only conversations with unread messages")
    p.add_argument("--limit", type=int, default=50)
    p.set_defaults(fn=cmd_list)

    p = sub.add_parser("show", parents=[common], help="print a conversation thread, oldest to newest")
    p.add_argument("id", type=int)
    p.add_argument("--limit", type=int, default=30, help="number of latest messages (default 30)")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("send", parents=[common], help="send a message now")
    p.add_argument("id", type=int)
    p.add_argument("text")
    p.set_defaults(fn=cmd_send)

    p = sub.add_parser("draft", parents=[common], help="save a draft reply for human approval")
    p.add_argument("id", type=int)
    p.add_argument("text")
    p.set_defaults(fn=cmd_draft)

    p = sub.add_parser("state", parents=[common], help="move a conversation to another state")
    p.add_argument("id", type=int)
    p.add_argument("state")
    p.add_argument("--reason")
    p.set_defaults(fn=cmd_state)

    # add_help=False: "-name" tokens (tag removals) must not be parsed as options.
    p = sub.add_parser("tag", parents=[common], add_help=False, help="add (+name) or remove (-name) tags")
    p.add_argument("id", type=int)
    p.add_argument("tags", nargs="*", help="+name to add, -name to remove")
    p.set_defaults(fn=cmd_tag)

    p = sub.add_parser("takeover", parents=[common], help="human takes over: agent_active=false, state in_progress")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_takeover)

    p = sub.add_parser("handback", parents=[common], help="hand the conversation back to the agent")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_handback)

    p = sub.add_parser("search", parents=[common], help="search message bodies")
    p.add_argument("query")
    p.add_argument("--account", type=int)
    p.set_defaults(fn=cmd_search)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if args.command == "tag":
        args.tags = [*args.tags, *extra]
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    try:
        with closing(core.db.connect()) as conn:
            args.fn(conn, args)
    except core.errors.WaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
