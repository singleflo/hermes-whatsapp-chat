"""CLI for Hermes (and humans) to operate the WhatsApp chat plugin.

Run with the plugin backend's Python: <data>/bin/wa <command>   (launcher written by the plugin's Install service)

    list [--state S] [--account ID] [--unread] [--limit N] [--list LIST|all] [--groups|--direct] [--json]
    show ID [--limit N] [--json]
    send ID TEXT              send now (only when the user explicitly asked)
    draft ID TEXT             store a draft for the human to review
    state ID STATE [--reason R]
    tag ID +a -b
    takeover ID | handback ID
    search QUERY [--account ID]
    set-list ID LIST [--this-number]   move a contact or group to a list (admin work personal unclassified ignored)
    silence ID [--hours N] | unsilence ID   no notifications for N hours (default: forever) / undo
    check PHONE [PHONE ...] [--account ID|LABEL] [--json]
                              is the number on WhatsApp? exit 0 all are, 2 when one is not
    send-to PHONE TEXT [--account ID|LABEL] [--name NAME] [--json]
                              send a first message to a number (creates the conversation); exit 2 not on WhatsApp
    draft-to PHONE TEXT [--account ID|LABEL] [--name NAME] [--json]
                              same, but store a draft for a human to approve
    jev show [--json]                         Jev exit conditions, their rules and the rules document
    jev on | jev off                          turn Jev classification on or off
    jev generate [FILE|-] [--no-check] [--apply] [--json]
                                              turn the Jev rules document into conditions + rules (preview;
                                              --apply stores them); FILE defaults to the stored document
    jev apply PLAN|- [--rules FILE] [--json]  apply a plan (a generate result or a bare plan)
    jev test TEXT | jev test --conversation ID [--json]

Author of send/draft/state changes: agent:$HERMES_PROFILE when set, else cli.
Exit codes: 0 ok, 1 error (message on stderr), 2 a number is not on WhatsApp. `--json` prints machine-readable output.
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
        list_id=args.list,
        kind="group" if args.groups else "direct" if args.direct else None,
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
    rows = [["ID", "ACCOUNT", "STATE", "LIST", "UNREAD", "AGE", "CONTACT", "LAST MESSAGE"]]
    for c in cards:
        who = c.get("contact_name") or c.get("phone") or ("Group" if c.get("is_group") else c["chat_jid"])
        if c.get("is_group"):
            who = f"[group] {who}"
        arrow = "<" if c.get("last_message_direction") == "in" else ">"
        draft = " [draft]" if c.get("has_draft") else ""
        silenced = " (silenced)" if c.get("silenced") else ""
        rows.append(
            [
                str(c["id"]),
                str(c.get("account_label") or c["account_id"]),
                c["state"] + ("*" if c.get("priority") else ""),
                str(c.get("list") or "-") + ("!" if c.get("list_scope") == "number" else ""),
                str(c.get("unread_count") or 0),
                fmt_age(c.get("age_seconds")),
                one_line(who, 28) + silenced,
                f"{arrow} {one_line(c.get('last_message_preview'), 60)}{draft}",
            ]
        )
    print(table(rows))
    print(f"{len(cards)} of {res['total']}")


def render_message(m: dict) -> str:
    media = "".join(f" [media: {x.get('type') or x.get('mime') or 'file'}]" for x in m.get("media") or [])
    status = m["status"]
    head = f"[{fmt_time(m['ts'])}] #{m['id']} {m['author']} ({status})"
    sender = m.get("sender_name") or (f"+{m['sender_phone']}" if m.get("sender_phone") else m.get("sender_jid"))
    if m["direction"] == "in" and sender:  # group message: who wrote it
        head += f" from {sender}" + (f" (+{m['sender_phone']})" if m.get("sender_name") and m.get("sender_phone") else "")
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
    who = c.get("contact_name") or c.get("phone") or ("Group" if c.get("is_group") else c["chat_jid"])
    print(f"{'Group' if c.get('is_group') else 'Conversation'} #{c['id']}: {who} ({c['chat_jid']})")
    print(f"Account: {acc.get('label') or c['account_id']} ({acc.get('kind') or '?'})")
    print(
        f"State: {c['state']}  agent_active: {c['agent_active']}  unread: {c['unread_count']}"
        f"  tags: {','.join(c.get('tags') or []) or '-'}"
    )
    silenced = "  silenced: " + (
        "forever" if c["silenced_until"] >= core.lists.SILENCE_FOREVER else f"until {fmt_time(c['silenced_until'])}"
    ) if c.get("silenced") else ""
    print(f"List: {core.lists.LIST_LABELS.get(c.get('list'), c.get('list'))} ({c.get('list')}"
          f"{', only this number' if c.get('list_scope') == 'number' else ''}){silenced}")
    if c.get("is_group"):
        people = c.get("participants") or []
        names = ", ".join(one_line(p.get("name") or (f"+{p['phone']}" if p.get("phone") else p["jid"]), 24) for p in people[:12])
        print(f"Participants: {len(people)}" + (f" ({names}{', ...' if len(people) > 12 else ''})" if names else ""))
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


def cmd_set_list(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    effective = core.lists.set_list(
        conn, args.id, args.list, "number" if args.this_number else "contact", now=int(time.time()), actor=author()
    )
    scope = "only this number" if args.this_number else "every number"
    emit(args, {"id": args.id, "list": effective, "scope": scope}, f"#{args.id} list={effective} ({scope})")


def cmd_silence(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    until = core.lists.silence(conn, args.id, args.hours, now=int(time.time()))
    text = "forever" if until >= core.lists.SILENCE_FOREVER else f"until {fmt_time(until)}"
    emit(args, {"id": args.id, "silenced_until": until}, f"#{args.id} silenced {text}")


def cmd_unsilence(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    core.lists.unsilence(conn, args.id, now=int(time.time()))
    emit(args, {"id": args.id, "silenced_until": None}, f"#{args.id} not silenced")


# --- new chat --------------------------------------------------------------------------


def resolve_account(conn: sqlite3.Connection, value: str | None) -> int | None:
    """``--account`` takes the numeric id or the label shown by ``wa list`` (case-insensitive)."""
    if value is None:
        return None
    text = value.strip()
    if text.isdigit():
        return int(text)
    for account in core.accounts.list_accounts(conn):
        if account["label"].casefold() == text.casefold():
            return account["id"]
    raise core.errors.NotFound(f"unknown account: {value}")


def cmd_check(conn: sqlite3.Connection, args: argparse.Namespace) -> int:
    res = core.newchat.check_numbers(conn, args.phones, resolve_account(conn, args.account))
    lines = []
    for r in res["results"]:
        if r["exists"]:
            line = f"{r['phone']}  on WhatsApp  ({r['jid']})"
            if r["conversation_id"]:
                line += f"  conversation #{r['conversation_id']}"
        else:
            line = f"{r['phone']}  NOT on WhatsApp"
        lines.append(line + ("  (this number)" if r["self"] else ""))
    emit(args, res, "\n".join(lines))
    return 0 if all(r["exists"] for r in res["results"]) else 2


def _start(conn: sqlite3.Connection, args: argparse.Namespace, mode: str) -> dict:
    who = author()
    return core.newchat.start_conversation(
        conn,
        phone=args.phone,
        text=args.text,
        mode=mode,
        account_id=resolve_account(conn, args.account),
        name=args.name,
        author=who,
        actor=who,
        now=int(time.time()),
    )


def cmd_send_to(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    res = _start(conn, args, "send")
    where = "new conversation" if res["created"] else "existing conversation"
    emit(args, res, f"sent #{res['message']['id']} to conversation {res['conversation']['id']} ({where})")


def cmd_draft_to(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    res = _start(conn, args, "draft")
    where = "new conversation" if res["created"] else "existing conversation"
    emit(args, res, f"draft #{res['message']['id']} in conversation {res['conversation']['id']} ({where})")


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


# --- jev -------------------------------------------------------------------------------


def _read_text(source: str) -> str:
    return sys.stdin.read() if source == "-" else Path(source).read_text(encoding="utf-8")


def _rule_line(rule: dict) -> str:
    flags = ("on" if rule["enabled"] else "off") + (", from rules" if rule["managed"] else "")
    return f"- {rule['name']} ({flags})"


def cmd_jev_show(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    st = core.jev_rules.status(conn)
    key = f"set ({st['key_source']})" if st["key_set"] else "not set"
    lines = [f"Jev: {'on' if st['enabled'] else 'off'}  key: {key}  model: {st['model']}", ""]
    lines += ["Rules document:", st["document"].strip() or "(empty)", "", "Conditions (the first whose score reaches its minimum wins):"]
    if not st["conditions"]:
        lines.append("(none: write the rules and run `jev generate --apply`)")
    for i, cond in enumerate(st["conditions"], 1):
        lines.append(f"{i}. {cond['label']} [{cond['id']}] min {cond['min_score']:g}")
        lines.append(f"   {cond['description']}")
        lines += [f"   {_rule_line(r)}" for r in cond["rules"]]
    lines.append("Else (no condition reached its minimum)")
    lines += [f"   {_rule_line(r)}" for r in st["else"]["rules"]]
    emit(args, st, "\n".join(lines))


def cmd_jev_toggle(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    enabled = args.command_value == "on"
    current = core.settings.get_settings(conn)
    saved = core.settings.save_settings(
        conn, current.model_copy(update={"jev": current.jev.model_copy(update={"enabled": enabled})}), int(time.time())
    )
    emit(args, {"enabled": saved.jev.enabled}, f"Jev is {'on' if saved.jev.enabled else 'off'}")


def render_plan(result: dict) -> str:
    plan = core.jev_rules.normalize_plan(result)
    checks = {(c["expected"], c["text"]): c for c in result.get("checks") or []}

    def examples(expected: str, texts: list[str]) -> list[str]:
        out = []
        for text in texts:
            c = checks.get((expected, text))
            if c is None:
                out.append(f"     - {text}")
                continue
            score = "" if c["score"] is None else f" {c['score']:.2f}"
            out.append(f"     {'✓' if c['ok'] else '✗'} {text} -> {c['exit']}{score}")
        return out

    def actions(items: list) -> str:
        return "; ".join(core.jev_rules.action_words(a) for a in items) or "none"

    lines = []
    for i, c in enumerate(plan.conditions, 1):
        lines += [f"{i}. {c.label} [{c.id}] min {c.min_score:g}", f"   {c.description}", f"   actions: {actions(c.actions)}"]
        if c.examples:
            lines += ["   examples:", *examples(c.id, c.examples)]
    lines += ["Else (no condition reached its minimum)", f"   actions: {actions(plan.else_actions)}"]
    if plan.else_examples:
        lines += ["   examples:", *examples(core.jev.ELSE, plan.else_examples)]
    if plan.notes:
        lines += ["Notes:", *[f"- {n}" for n in plan.notes]]
    if result.get("check_error"):
        lines.append(f"Check: {result['check_error']}")
    if result.get("refined"):
        lines.append("Descriptions sharpened once after the Jev check (more examples pass).")
    return "\n".join(lines)


def render_applied(applied: dict) -> str:
    exits = applied["settings"]["jev"]["exits"]
    lines = [f"Applied {len(exits)} conditions and {len(applied['rules'])} rules."]
    lines += [f"{i}. {e['label']} [{e['id']}] min {e['min_score']:g}" for i, e in enumerate(exits, 1)]
    lines += [f"- {r['name']} ({'on' if r['enabled'] else 'off'})" for r in applied["rules"]]
    return "\n".join(lines)


def cmd_jev_generate(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    document = _read_text(args.file) if args.file else core.settings.get_settings(conn).jev.document
    result = core.jev_rules.generate(conn, document, check=not args.no_check)
    applied = core.jev_rules.apply(conn, document, result["plan"], int(time.time())) if args.apply else None
    if args.json:
        emit(args, {"generate": result, "apply": applied} if args.apply else result, "")
        return
    print(render_plan(result))
    if applied is not None:
        print("\n" + render_applied(applied))


def cmd_jev_apply(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    plan = json.loads(_read_text(args.plan))
    document = _read_text(args.rules) if args.rules else core.settings.get_settings(conn).jev.document
    applied = core.jev_rules.apply(conn, document, plan, int(time.time()))
    emit(args, applied, render_applied(applied))


def cmd_jev_test(conn: sqlite3.Connection, args: argparse.Namespace) -> None:
    if (args.text is None) == (args.conversation is None):
        raise core.errors.Invalid("pass exactly one of TEXT or --conversation ID")
    if args.text is not None:
        res = core.jev.score_text(conn, args.text)
    else:
        res = core.jev.score_conversation(conn, args.conversation)
    emit(args, res, f"{res['summary']}\n(model {res['model']}, {res['latency_ms']} ms)")


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
    p.add_argument("--list", help="admin|work|personal|unclassified|ignored|all (default: every list except ignored)")
    kind = p.add_mutually_exclusive_group()
    kind.add_argument("--groups", action="store_true", help="only WhatsApp groups")
    kind.add_argument("--direct", action="store_true", help="only direct chats")
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

    p = sub.add_parser("set-list", parents=[common], help="move a contact or group to a list")
    p.add_argument("id", type=int)
    p.add_argument("list", help="admin|work|personal|unclassified|ignored")
    p.add_argument("--this-number", action="store_true", help="only this conversation (number), not the contact")
    p.set_defaults(fn=cmd_set_list)

    p = sub.add_parser("silence", parents=[common], help="no notifications for a conversation")
    p.add_argument("id", type=int)
    p.add_argument("--hours", type=int, help="1-8760; default: forever")
    p.set_defaults(fn=cmd_silence)

    p = sub.add_parser("unsilence", parents=[common], help="notifications on again")
    p.add_argument("id", type=int)
    p.set_defaults(fn=cmd_unsilence)

    p = sub.add_parser("search", parents=[common], help="search message bodies")
    p.add_argument("query")
    p.add_argument("--account", type=int)
    p.set_defaults(fn=cmd_search)

    p = sub.add_parser("check", parents=[common], help="is a number on WhatsApp? (exit 2 when not)")
    p.add_argument("phones", nargs="+", metavar="PHONE")
    p.add_argument("--account", help="account id or label")
    p.set_defaults(fn=cmd_check)

    for name, fn, what in (
        ("send-to", cmd_send_to, "send a first message to a number"),
        ("draft-to", cmd_draft_to, "save a draft for a number"),
    ):
        p = sub.add_parser(name, parents=[common], help=f"{what} (creates the conversation if needed)")
        p.add_argument("phone")
        p.add_argument("text")
        p.add_argument("--account", help="account id or label")
        p.add_argument("--name", help="contact name for a new conversation")
        p.set_defaults(fn=fn)

    jev = sub.add_parser("jev", help="Jev exit conditions: rules document, conditions, rules")
    jsub = jev.add_subparsers(dest="jev_command", required=True)

    p = jsub.add_parser("show", parents=[common], help="status, rules document, conditions and their rules")
    p.set_defaults(fn=cmd_jev_show)

    for value in ("on", "off"):
        p = jsub.add_parser(value, parents=[common], help=f"turn Jev {value}")
        p.set_defaults(fn=cmd_jev_toggle, command_value=value)

    p = jsub.add_parser("generate", parents=[common], help="turn the rules document into conditions and rules")
    p.add_argument("file", nargs="?", help="rules document (- = stdin); default: the stored document")
    p.add_argument("--no-check", action="store_true", help="do not score the examples with Jev")
    p.add_argument("--apply", action="store_true", help="store the result (document, conditions, rules)")
    p.set_defaults(fn=cmd_jev_generate)

    p = jsub.add_parser("apply", parents=[common], help="apply a plan (generate result or bare plan JSON)")
    p.add_argument("plan", help="plan file (- = stdin)")
    p.add_argument("--rules", help="rules document file; default: the stored document")
    p.set_defaults(fn=cmd_jev_apply)

    p = jsub.add_parser("test", parents=[common], help="score a message (or a conversation's latest) now")
    p.add_argument("text", nargs="?")
    p.add_argument("--conversation", type=int, help="conversation id")
    p.set_defaults(fn=cmd_jev_test)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args, extra = parser.parse_known_args(argv)
    if args.command == "tag":
        args.tags = [*args.tags, *extra]
    elif extra:
        parser.error(f"unrecognized arguments: {' '.join(extra)}")
    code = 0
    try:
        with closing(core.db.connect()) as conn:
            code = args.fn(conn, args) or 0
    except core.errors.NotOnWhatsApp as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    except core.errors.WaError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return code


if __name__ == "__main__":
    sys.exit(main())
