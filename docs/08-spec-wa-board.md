# 08 — Functional spec: the conversations board (wa-board)

The converged design from the 2026-09-29 session. This is the WHAT; the HOW is
in files 01-07. OMP implements against this spec.

## Context (one paragraph)

Roberto runs a WhatsApp assistant on a dedicated number via the Hermes native
Baileys bridge (bot mode) on the VPS gateway. Inbound messages flow through a
gating pipeline (policy → archive → coalesce → deterministic automations →
cheap classifier → full agent). Every message in/out is archived to SQLite
deterministically (zero LLM). The classifier tags conversation state. This
plugin is the **management view**: a kanban board of conversations, one card per
contact, showing only what needs active attention — plus the controls (mute,
takeover, escalate). It is a VIEW + state controls, NOT the data pipeline.

## Board model

### Columns (conversation states)

```
NUOVA → IN_GESTIONE → IN_ATTESA → CHIUSA
                 └──────→ MUTATA (snooze; auto-returns or reopens on new msg)
+ fallback column for unknown states (never drop a card)
```

- **NUOVA**: unseen chat / untriaged.
- **IN_GESTIONE**: agent (or human) actively working it.
- **IN_ATTESA**: waiting on the contact (follow-up timers live here).
- **MUTATA**: muted until `muted_until` or a substantive new message reopens it.
- **CHIUSA**: done; leaves the default view (filter/archive toggle).
- Unknown state → fallback column (kanban plugin's lesson: "a status missing
  here gets mis-bucketed" — make the mapping exhaustive + fallback).

### Card payload (GET /board)

```
chat_jid, contact_name, phone, state, priority,
last_message_preview (≤200 chars), last_message_at, age_seconds,
unread_count, agent_active (bool), muted_until?, tags (classifier labels)
```

### Volume requirement (the 300-400 conversation question)

The board shows **only active states** (NUOVA/IN_GESTIONE/IN_ATTESA + MUTATED
counted). Design target: ≤30 cards visible; CHIUSA + full archive reachable via
filter/search (backed by the DB, not the board). `/stats` exposes per-state
counts so the UI can render "CHIUSE: 312" as a badge, not as cards.

## Routes (backend contract)

| Route | Semantics |
|---|---|
| `GET /board?include_closed=false` | columns + cards (aggregate queries, no N+1); derived ages server-side |
| `GET /chats/{chat_jid}` | full recent thread + contact metadata + state history |
| `POST /chats/{chat_jid}/state` | `{state, muted_until?}` — valid transitions only; 409 with actionable reason on refusal |
| `POST /chats/{chat_jid}/mute` | convenience: state=MUTATA + `muted_until` |
| `GET /stats` | per-state counts, oldest-unanswered age |
| `GET /health` | DB reachable, counts — the desktop half's degradation signal |
| `WS /events` (dashboard) / `broadcast_plugin_event` (desktop) | live updates on new message / state change |

## UI requirements

### Both halves

- Kanban columns, drag-drop between allowed states (invalid drops → toast with
  the reason — kanban's 409 pattern).
- Card: contact, preview, relative time, unread badge, urgency color from
  classifier tag (theme vars, no hardcoded colors).
- Detail drawer: thread, state history, mute/takeover/escalate actions.
- Search/filter (saved views phase 2: "in attesa >24h", "nuove non triaggiate").
- Works with backend down: cached data + clear error state (remote gateway
  reality).

### Desktop half specifics

- Full page (`ROUTES_AREA` + `SIDEBAR_NAV_AREA`, codicon `comment-discussion`)
  + palette command "Open Conversations".
- Statusbar chip: NUOVA count + oldest unanswered age (from `/stats`, poll ≤
  few seconds or event-driven invalidation via `host.onEvent`).
- Native notification on new NUOVA when app unfocused (`ctx.os.notify`).
- React Query for all data; `ctx.socket` with polling fallback.

### Dashboard half specifics

- Tab after Skills; IIFE bundle; `SDK.components.*` primitives.
- Live updates via `WS /events` (kanban `_EventTail` pattern) with polling fallback.

## State transitions (the guarded set)

```
NUOVA      → IN_GESTIONE, MUTATA, CHIUSA
IN_GESTIONE→ IN_ATTESA, CHIUSA, MUTATA
IN_ATTESA  → IN_GESTIONE (reopen), CHIUSA, MUTATA
MUTATA     → IN_GESTIONE (manual reopen), NUOVA (auto on substantive msg)
CHIUSA     → IN_GESTIONE (manual reopen only)
```

Every transition writes an audit row (who: user/agent/auto, when, reason).
Invalid transition → 409 naming what's wrong.

## Out of scope (explicit)

- Message sending/replying from the board (the session agent / bridge owns that).
- The pipeline itself (hooks, classifier, archive writer — separate components).
- Kanban worker dispatch (unless a later phase adds follow-up cards; then: ONE
  card per contact, comment-thread continuity — see
  skills/hermes-entity-automation.md).

## Data contract (DB the plugin reads)

Owned by the pipeline, not this plugin. The plugin only READS `messages` +
`conversations` and WRITES conversation-state transitions (its own
`conversation_state_log` table or the pipeline's, decided at implementation).
Minimum fields the plugin depends on:

```
conversations(chat_jid PK, contact_name, phone, state, priority, muted_until,
              last_message_at, unread_count, updated_at)
messages(id, chat_jid FK, direction in/out, body, ts, meta)
conversation_state_log(id, chat_jid, from_state, to_state, actor, reason, at)
```

## Definition of done

1. `pytest tests/` green — seams in 05-testing-tdd.md covered.
2. Installed as unified package on the Mac: dashboard tab renders the board
   from a seeded DB; drag-drop transitions persist and appear in the log.
3. Desktop half toggled on: page + statusbar chip + notifications working.
4. Backend down: both halves degrade to error state, no crash.
5. 400-conversation seeded DB: board <30 active cards, `/board` p95 < 300ms
   (aggregate queries, no N+1).
