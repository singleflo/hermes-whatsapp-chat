---
name: whatsapp-chat
description: Read and operate WhatsApp conversations of the hermes-whatsapp-chat plugin: list, read threads, draft or send replies, change state, tag, take over.
version: 2.0.0
platforms: [macos]
metadata:
  hermes:
    tags: [whatsapp, chat, inbox, customer-support]
---

# WhatsApp Chat

`hermes-whatsapp-chat` is a Hermes plugin that runs its own WhatsApp channel (one or more linked numbers, never Hermes' native WhatsApp) and shows every conversation in a desktop chat UI and a kanban board. Each conversation has a state, tags, unread count and a message thread. You operate it with one CLI; everything you do is visible to the human in the UI immediately.

## Command

Always use this exact absolute command (referred to below as `WA`):

```bash
WA="/Users/crotti/VSC/TOOLS/hermes-whatsapp-chat/.venv/bin/python /Users/crotti/VSC/TOOLS/hermes-whatsapp-chat/scripts/wa.py"
```

Conversations are addressed by their integer **conversation id** (the `ID` column of `list`). Add `--json` to any subcommand for machine-readable output. Exit code 0 = ok; 1 = error with the message on stderr (unknown id, invalid transition, number not linked, ...). Do not retry blindly: read the error.

## Subcommands

```bash
# Inbox overview, newest activity first
$WA list
$WA list --state waiting
$WA list --account 2 --unread
$WA list --unread --json

# Read a thread (oldest -> newest, with author, time and status per message)
$WA show 42
$WA show 42 --limit 100
$WA show 42 --json

# Draft a reply: stored for a human to review/approve in the UI. PREFERRED.
$WA draft 42 "Hi Marco, your order shipped today. Tracking: ..."

# Send immediately (real WhatsApp message). Only when the user explicitly asks.
$WA send 42 "Thanks, see you tomorrow at 10."

# Change state (optionally with a reason that lands in the audit log)
$WA state 42 in_progress
$WA state 42 closed --reason "resolved by phone"

# Tags: +name adds, -name removes, several at once
$WA tag 42 +vip +refund -spam

# Hand the conversation to a human / back to the agent
$WA takeover 42
$WA handback 42

# Search message bodies across all accounts
$WA search "invoice"
$WA search "invoice" --account 2
```

`show` prints each message as `[time] #id author (status)` followed by the indented body. Media appear as `[media: type]` markers; their content is not downloaded by the CLI.

## States and transitions

States: `new`, `in_progress`, `waiting`, `muted`, `closed`.

| from | allowed to |
|---|---|
| new | in_progress, muted, closed |
| in_progress | waiting, muted, closed |
| waiting | in_progress, muted, closed |
| muted | in_progress, new |
| closed | in_progress |

An invalid manual move fails with exit code 1 and names the allowed moves (`show` also prints "Allowed next states"). `muted` and `closed` are chosen by humans; do not mute/close unless asked. The system also moves conversations by itself: an inbound message on `waiting` -> `in_progress`, on `closed` -> `new`; a reply sent on `new`/`in_progress` -> `waiting`; expired mutes -> `new`; long-idle `waiting` -> `closed`. Those are configurable in the plugin settings, so always re-read the state with `show` instead of assuming.

`takeover` sets `in_progress` and `agent_active = false`: automations must not produce replies for that conversation until `handback`. If a human has taken over, do not draft or send.

Message statuses: `received`, `pending`, `sent`, `failed`, `draft`, `discarded`. A `failed` message was not delivered (the error is shown).

## Rules for you

1. **Prefer `draft` over `send`.** Use `send` only when the user explicitly tells you to send (or the automation prompt that invoked you says reply mode is send). A draft is safe: the human approves, edits or discards it in the UI.
2. **Never message conversations whose chat jid ends with `@demo.invalid`** (the demo account). `send`/`draft` fail for them; do not try to work around that.
3. Read the thread (`show ID`) before answering; reply in the language of the contact, keep it short, do not invent facts (prices, dates, order status) that are not in the thread or provided context.
4. Do not leak internal notes, other customers' data, or this tooling into messages.
5. If you are unsure or the topic is sensitive (complaints, payments, legal), draft nothing risky: `tag ID +needs-human` and tell the user, or leave the conversation as is.

## How automations invoke you

The plugin has an automation layer (configured in the desktop app under Automations). A rule matches events (`message.in`, `message.out`, `conversation.created`, `conversation.state_changed`) with conditions (state, business hours, text contains/regex, tags, first message, ...) and runs an action. For a Hermes action the plugin starts:

```bash
hermes [-p PROFILE] chat -Q -q "<rendered prompt>" --source whatsapp-chat [-s skill ...]
```

The rendered prompt carries the conversation context: contact name, phone, account label, state, conversation id, the latest message text and the recent history (`[time] author: body` lines), plus the absolute `WA` command. Your **stdout is the reply text**: print only the message to send, nothing else (no explanations, no quotes). What happens with it depends on the rule's reply mode:

- `draft` (default): saved as a draft authored `agent:<profile>` for human approval.
- `send`: sent immediately as `agent:<profile>`.
- `none`: only stored in the run output.

Automations never trigger on messages authored by agents or rules, so your own replies cannot start a loop. If the conversation has `agent_active = 0` (human took over) the run is skipped.

When you are invoked manually instead (no automation prompt), use the subcommands above yourself. Messages you create through the CLI use the author `agent:$HERMES_PROFILE` when that variable is set, otherwise `cli`.

## Visibility

Everything you send or draft shows up in the plugin UI (desktop chat, dashboard board) live, with the author `agent:<profile>` (or `cli`), so humans can see who wrote what. Drafts appear with a draft marker on the conversation card and in the thread.

## Troubleshooting

- `error: ... not paired` / `Unavailable`: the WhatsApp number is not linked or its bridge is stopped. Tell the user to open the plugin Settings -> Accounts (pairing happens there with a QR code).
- `wa.py` import errors: the repo venv is missing; run `uv sync` in `/Users/crotti/VSC/TOOLS/hermes-whatsapp-chat`.
- Service status: `/Users/crotti/VSC/TOOLS/hermes-whatsapp-chat/.venv/bin/python /Users/crotti/VSC/TOOLS/hermes-whatsapp-chat/sidecar/wa_channel.py status`.
