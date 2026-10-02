---
name: hermes-entity-automation
description: "Webhook+Kanban design for always-on lead/invoice agents."
version: 1.0.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [webhook, kanban, profiles, automation, crm, odoo, multi-agent, continuity]
    related_skills: [odoo-assistant-mcp, kanban-orchestrator, kanban-worker, hermes-profiles]
---

# Hermes Entity Automation — Webhook + Kanban + Profiles

Architecture pattern for building "always-on staff" automations (lead qualifier,
invoice chaser, order handler, project watcher) natively on Hermes, without
reaching for an external multi-agent harness (Munder Difflin, custom
file-based hive, etc.).

## The 3 native building blocks

| Role in a typical multi-agent harness | Hermes-native equivalent |
|---|---|
| Polling loop / watermark file | **Webhook platform route** (`platforms.webhook`) — event-driven, not poll-based |
| Named specialist agent (e.g. "lead-agent", "invoice-agent") | **Hermes profile** with a SOUL.md encoding the domain protocol |
| Inbox/outbox files, board.md, task ledger | **Kanban board** (`kanban_*` tools) — durable SQLite task queue, comment thread, parent links |
| Circuit breaker / anti-loop | Kanban dispatcher's built-in retry/reclaim/failure_limit |
| Shared semantic memory (e.g. Chroma/MemPalace) | Per-profile `fact_store` / memory — for durable domain knowledge, NOT per-entity state |

Zero custom infrastructure needed — all three are core Hermes features. The
actual engineering work is: (1) write the domain protocol as profile
SOUL.md/prompt content, (2) map entity events to webhook routes, (3) get the
entity-continuity pattern right (see below — this is the part that is easy
to get wrong).

## The entity-continuity pattern (the part that's easy to get wrong)

**A Kanban worker is a fresh OS process on every spawn — it does NOT resume
a previous LLM conversation.** There is no `--resume`/`--continue` semantics
between task runs. If you create a new Kanban task per webhook event, each
worker sees only that one event's payload and nothing else that happened to
the same entity before it. This is the mistake to avoid.

The fix: **one Kanban task per business entity, kept open across its whole
lifecycle**, not one task per event.

- **First event for an entity** (e.g. lead created): `kanban_create` with an
  `idempotency_key` derived from a stable entity identifier (e.g.
  `lead-<crm_lead_id>` or, better, a dedup key for the underlying person —
  phone/email — so that multiple CRM records for the same contact collapse
  onto one task, mirroring standard CRM dedup practice).
- **Every subsequent event for the same entity** (record modified, customer
  replied, follow-up due): look up the existing open task (by idempotency
  key or `kanban_list` filtered on title/tenant), then `kanban_comment(...)`
  onto it — never `kanban_create` again. If the task is `blocked`/waiting,
  `kanban_unblock()` it so the dispatcher respawns a worker.
- **How continuity actually happens**: "when a worker is (re-)spawned it
  reads the full comment thread as part of its context" (Hermes Kanban
  docs). The comment thread IS the conversation history for that entity —
  write to it like you'd want a human handing off a shift to read it.
- **Parent-link handoff** (`## Parent task results` in child worker
  context) is a *different* mechanism — a one-time summary+metadata handoff
  from a completed task to a new one. Use it for pipeline stages (qualify →
  send → review), not for continuity within one entity's lifecycle.
- **Timed follow-ups with no inbound event** (e.g. D+2/D+5 cadence with no
  webhook to trigger them): either a linked child task with `scheduled_at`,
  or a lightweight cron that just posts a `kanban_comment("D+2 follow-up
  due")` + `kanban_unblock()` on the existing entity task — do not spin up
  a parallel polling agent for this, keep the single entity task as the
  source of truth.

## Real-time conversation assistants: Kanban manages, the session answers

When the entity is a live chat (WhatsApp/Telegram assistant via a native
gateway adapter), do NOT route replies through Kanban workers — a worker is a
fresh process per spawn with no conversation resume, plus dispatcher latency.
The gateway session IS the live conversation. Kanban is the management layer
only: one card per contact (entity-continuity pattern), state overview, human
takeover (block card = mute auto-reply), scheduled follow-ups, with a short
summary comment mirrored onto the card at every turn. Gate inbound messages
through a classifier pipeline so only a minority wake the full agent, and run
outbound follow-ups from cron with cooldown/dedup guardrails. Full design:
`references/conversation-assistant-pattern.md`.

## Building the webhook side

```yaml
# config.yaml
platforms:
  webhook:
    enabled: true
    extra:
      port: 8644
      routes:
        odoo-lead-changed:
          prompt: |
            Lead {event_data.id} created/changed: {event_data.description}
          deliver: log
```

For filtering noisy upstream webhooks before they reach Hermes (e.g. only
forward `crm.lead` writes, verify HMAC, normalize payload shape), the
third-party `hermes-webhook-router` package (MIT, pip-installable, `hermes
plugins enable webhook-router`) sits in front of the Hermes webhook endpoint
as a pre-filter — useful when the upstream system fires many irrelevant
events per real one.

The webhook handler itself is where the entity-lookup-or-create decision
above must live — write it explicitly, it's not automatic.

## Check for vertical solutions first
The pattern above is horizontal infrastructure. For some domains a
ready-made vertical product already exists — for sales/SDR work,
`b2b-sdr-agent-template` (PulseAgent) ships a 10-stage pipeline with an
official Hermes skill port (`hermes skills install
github:iPythoning/b2b-sdr-hermes-skill`). Before building domain logic
from scratch, check for such verticals and evaluate adapt-then-extend vs
build-from-pattern. See `references/harness-alternatives.md` for the
evaluation notes.

## Domain protocol lives in the profile, not in files

Where a file-based hive stores its protocol as a markdown file every agent
re-reads each loop (`PROTOCOL.md`, `lead-agent-protocol.md`), a Hermes
profile encodes it directly as SOUL.md / system-prompt content for a
profile created via `hermes profile create <name> --no-skills`. See
`hermes-profiles` skill for profile creation mechanics. Keep the protocol
concrete and operational (channel choice, cadence, dedup rules, standard
send sequence) — see `references/lead-protocol-example.md` for a real,
human-approved, tested example (WhatsApp/email lead qualification for an
Odoo CRM) that can be adapted to invoices/orders/projects by swapping the
domain rules.

## Pitfalls

- **Assuming Kanban gives session continuity for free.** It doesn't —
  continuity is the comment thread, which you must write to deliberately on
  every event, not the underlying LLM session.
- **One task per event instead of per entity.** Fragments an entity's
  history across N disconnected tasks; the worker on event 3 never sees
  events 1-2.
- **Confusing parent-link handoff with entity continuity.** Parent links
  are for pipeline stage handoff (one-time summary), not for an entity's
  ongoing conversation history.
- **Putting per-entity state in profile memory/fact_store.** That store is
  for durable cross-entity domain knowledge (patterns, conventions learned
  over time), not for "what happened to lead 758" — that belongs on the
  entity's Kanban task/comment thread, or in the source-of-truth system
  itself (e.g. the CRM) if one exists.
- **Routing real-time chat replies through Kanban workers.** Workers spawn
  fresh per task (no conversation resume) and add dispatcher latency; the
  gateway session is the live conversation. Kanban is the management layer
  only — see `references/conversation-assistant-pattern.md`.

## Rev.7 pattern: factual archive + context-assembly manager

Validated design evolution for the always-on agent that must know the
instance's HISTORY, not just its current data (the "agent of month 2 answers
with connections month 1 could not see" goal):

1. **Factual archive, populated every round** — one row per fact: (fact →
   query that proves it → date → open case id). Facts without cited evidence
   are DISCARDED on read: this is also the injection defense (assertions
   without proof cannot poison the archive). Case = an open container of a
   business situation ("client X relationship failing, 3 symptoms"), NOT an
   event; cases absorb later findings instead of spawning new ones.
2. **Manager = context assembler, NOT a smarter model.** It is a rendering
   step, the same engine in a different role: it reads the archive and hands
   each operator the facts relevant to its task (open cases touched, findings
   still true, client history) and is the only role that answers the
   cross-domain question ("are these connected?"). Never give it decision
   authority over operators — a multi-role hierarchy with authority is the
   spec shape that collapsed under its own contradictions in an earlier
   design (each new rule contradicted ~2-3 existing ones; contradictions grew
   every review round).
3. **Per-operator procedural memory** — each domain operator's rules file
   grows with its own discoveries (banned fields, non-canonical state values,
   verified dead-letter rules); the manager consults it but does not teach it.
4. **No fact ever grants permission** — archive/case/rules stores are all
   knowledge, never authority. Promotion stays measured on outcomes.

This maps 1:1 onto Hermes's own Kanban internals (verified in hermes-agent
source): open case = root card kept alive across child fan-out
(`kanban_decompose`); manager = `build_worker_context()` — a bounded context
renderer (prior attempts, parent results, role history, comments), not an
orchestrator brain; per-operator memory = `_ctx_role_history` (assignee's
recent completed runs injected as "implicit role continuity"); anti-amnesia =
`block_recurrences` counter that resets only on successful completion
("resetting on unblock is exactly the amnesia that let the loop run
unbounded"); state/history separation = `tasks` vs append-only `task_events`
+ `task_runs`; per-role capability limits = tool gating (`kanban_list` hidden
from task workers); no unassigned child = `default_assignee` rewrite.
When building natively, reuse those primitives instead of re-deriving:
one open card per case, comment thread as case history, context renderer for
dispatch, typed block reasons (`dependency|needs_input|capability|transient`)
as the operator's declaration vocabulary.

## When NOT to use this pattern

If the "agent" only needs to react to a single event with no memory of
prior events on the same entity (e.g. a one-shot notification), a plain
webhook route with `deliver` straight to a message/log is enough — don't
build a Kanban entity task for stateless reactions.

## Related evaluation notes

`references/harness-alternatives.md` has short, dated notes on external
multi-agent harnesses evaluated as alternatives/prior art (Munder Difflin,
Claude Squad, Vibe Kanban, Spacebot) — useful if revisiting "should this run
on an external harness instead of Hermes-native" for a different use case
(e.g. true multi-person team collaboration, which Hermes Kanban does not
target).
