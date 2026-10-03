---
name: whatsapp-chat
description: Read and operate WhatsApp conversations of the hermes-whatsapp-chat plugin (list, read threads, draft or send replies, change state, tag, take over) and write its Jev rules, which classify every incoming message and decide what happens (take over, escalate, agent draft, ...).
version: 2.1.0
platforms: [macos, linux, windows]
metadata:
  hermes:
    tags: [whatsapp, chat, inbox, customer-support]
---

# WhatsApp Chat

`hermes-whatsapp-chat` is a Hermes plugin that runs its own WhatsApp channel (one or more linked numbers, never Hermes' native WhatsApp) and shows every conversation in a desktop chat UI and a kanban board. Each conversation has a state, tags, unread count and a message thread. You operate it with one CLI; everything you do is visible to the human in the UI immediately. It can also classify every incoming message with **Jev** following rules the user writes in plain words (see "Jev rules" below).

## Command

Always use this exact absolute command (every example below uses it):

```bash
{{WA_CLI}}
```

Conversations are addressed by their integer **conversation id** (the `ID` column of `list`). Add `--json` to any subcommand for machine-readable output. Exit code 0 = ok; 1 = error with the message on stderr (unknown id, invalid transition, number not linked, ...). Do not retry blindly: read the error.

## Subcommands

```bash
# Inbox overview, newest activity first
{{WA_CLI}} list
{{WA_CLI}} list --state waiting
{{WA_CLI}} list --account 2 --unread
{{WA_CLI}} list --unread --json

# Read a thread (oldest -> newest, with author, time and status per message)
{{WA_CLI}} show 42
{{WA_CLI}} show 42 --limit 100
{{WA_CLI}} show 42 --json

# Draft a reply: stored for a human to review/approve in the UI. PREFERRED.
{{WA_CLI}} draft 42 "Hi Marco, your order shipped today. Tracking: ..."

# Send immediately (real WhatsApp message). Only when the user explicitly asks.
{{WA_CLI}} send 42 "Thanks, see you tomorrow at 10."

# Change state (optionally with a reason that lands in the audit log)
{{WA_CLI}} state 42 in_progress
{{WA_CLI}} state 42 closed --reason "resolved by phone"

# Tags: +name adds, -name removes, several at once
{{WA_CLI}} tag 42 +vip +refund -spam

# Hand the conversation to a human / back to the agent
{{WA_CLI}} takeover 42
{{WA_CLI}} handback 42

# Search message bodies across all accounts
{{WA_CLI}} search "invoice"
{{WA_CLI}} search "invoice" --account 2
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

The plugin has an automation layer (configured in the desktop app under Automations). A rule matches events (`message.in`, `message.out`, `conversation.created`, `conversation.state_changed`, `conversation.classified` from Jev) with conditions (state, business hours, text contains/regex, tags, first message, Jev condition, ...) and runs an action. The rules generated from the Jev rules document are ordinary automation rules on `conversation.classified`. For a Hermes action the plugin starts:

```bash
hermes [-p PROFILE] chat -Q -q "<rendered prompt>" --source whatsapp-chat [-s skill ...]
```

The rendered prompt carries the conversation context: contact name, phone, account label, state, conversation id, the latest message text and the recent history (`[time] author: body` lines), plus the absolute CLI command (`{{WA_CLI}}`). Your **stdout is the reply text**: print only the message to send, nothing else (no explanations, no quotes). What happens with it depends on the rule's reply mode:

- `draft` (default): saved as a draft authored `agent:<profile>` for human approval.
- `send`: sent immediately as `agent:<profile>`.
- `none`: only stored in the run output.

Automations never trigger on messages authored by agents or rules, so your own replies cannot start a loop. If the conversation has `agent_active = 0` (human took over) the run is skipped.

When you are invoked manually instead (no automation prompt), use the subcommands above yourself. Messages you create through the CLI use the author `agent:$HERMES_PROFILE` when that variable is set, otherwise `cli`.

## Jev rules (classify incoming messages, decide what happens)

Jev (TypeSafe) scores every incoming WhatsApp message against a short list of **conditions** in about half a second, without an LLM. The first condition in the list whose score reaches its minimum is chosen; when none does, **Else** is chosen. Then the **actions** of the chosen condition run (take over for a person, escalate, Hermes agent draft, fixed reply, set state, add tags).

The user does not edit conditions one by one. They keep ONE Markdown document, the **Jev rules** (like USER.md), in their own words and language. From it the plugin generates the conditions and their actions with the profile's default model (one fast call, JSON only), checks them with Jev on example messages, and applies them. Re-applying replaces only the rules generated from the document; rules the user made by hand in Automations are never touched.

```bash
# Current state: on/off, API key, the rules document, the active conditions and their actions
{{WA_CLI}} jev show
{{WA_CLI}} jev show --json

# Preview the conditions generated from a rules file (nothing is saved); every example is checked with Jev (✓/✗)
{{WA_CLI}} jev generate /tmp/jev-rules.md
# Same, and apply it (stores the document, the conditions and the generated rules)
{{WA_CLI}} jev generate /tmp/jev-rules.md --apply
# Regenerate from the stored document
{{WA_CLI}} jev generate --apply

# Apply a plan you reviewed or edited (output of `generate --json`, or a bare plan)
{{WA_CLI}} jev generate /tmp/jev-rules.md --json > /tmp/jev-plan.json
{{WA_CLI}} jev apply /tmp/jev-plan.json --rules /tmp/jev-rules.md

# Try a message against the active conditions, or a conversation's latest incoming message
{{WA_CLI}} jev test "a che punto è la mia pratica?"
{{WA_CLI}} jev test --conversation 42

# Turn classification on or off
{{WA_CLI}} jev on
{{WA_CLI}} jev off
```

### When the user asks you to create or change the rules

1. `{{WA_CLI}} jev show` and read the current document: you extend or edit it, you do not throw it away unless asked.
2. Write the whole document to a temporary file in the user's language. One `##` section per situation, in priority order (the first matching condition wins), each with what the contact writes and what should happen. End with a section for everything else. Example:

   ```markdown
   # WhatsApp rules

   ## Needs a person
   - The customer complains, is angry or reports something urgent (system down, error in production): I take it over and mark it urgent.
   - Quotes, prices, discounts, contracts, invoices: I handle them myself.

   ## The agent can answer
   - Routine questions (opening hours, how something works, status of a request, which documents to send): the agent writes a short, polite draft and never invents dates or prices.

   ## No answer needed
   - Greetings, thanks, "ok", emoji: close the conversation without answering.

   ## Everything else
   - I take the conversation over.
   ```

   Say what should happen with these words, which map to actions: take over / I handle it (takeover), urgent (escalate), the agent drafts (agent draft, approved by a person), the agent answers by itself (agent reply, sent at once: only when the user explicitly wants automatic answers), always answer "..." (fixed reply, sent at once), close / waiting / in progress (set state), tag X (add tags).
3. `{{WA_CLI}} jev generate FILE` and read the preview: every example should be ✓. A ✗ means two conditions overlap or a description is vague: sharpen the wording in the document (name the words or requests that signal each situation, keep one situation per section) and generate again. Report the notes the generator prints.
4. Show the user the conditions, their actions and the checks. Apply only when they agree, or when they asked you to apply directly: `{{WA_CLI}} jev generate FILE --apply`.
5. Confirm with two or three realistic messages: `{{WA_CLI}} jev test "..."`.
6. Classification runs only while Jev is on and an API key is set (`jev show`). Turn it on with `{{WA_CLI}} jev on` only when the user asks; the API key is entered by the user in the plugin Settings → Jev (never ask for it in chat, never print it).

Notes: generated actions that send messages (`agent reply`, `fixed reply`) reach real contacts at once; prefer drafts unless the user wants automatic answers. When Hermes runs as a Jev action, its prompt contains a `Jev:` block with the chosen condition and every score: use it to understand why you were called.

## Visibility

Everything you send or draft shows up in the plugin UI (desktop chat, dashboard board) live, with the author `agent:<profile>` (or `cli`), so humans can see who wrote what. Drafts appear with a draft marker on the conversation card and in the thread.

## Troubleshooting

- `error: ... not paired` / `Unavailable`: the WhatsApp number is not linked or its bridge is stopped. Tell the user to open the plugin Settings -> Accounts (pairing happens there with a QR code).
- The CLI fails to start or reports import errors: the launcher it runs through is stale (for example after a Hermes update). Tell the user to open the plugin Settings -> Numbers and press **Reinstall** (or **Install skill**); that rewrites the launcher and this skill.
- Service status: the plugin UI (Settings -> Numbers) shows whether the channel service is running; if it is not, tell the user to press **Install service** there.
