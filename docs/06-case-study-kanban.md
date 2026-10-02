# 06 — Case study: the bundled kanban dashboard plugin

Primary reference, read in full on 2026-09-29:
`/Users/crotti/.hermes/hermes-agent/plugins/kanban/dashboard/` — this is
Hermes' own board UI, the closest existing thing to hermes-whatsapp-chat.

## Files

```
plugins/kanban/
├── dashboard/
│   ├── manifest.json      # { name: "kanban", label: "Kanban", icon: "Package",
│   │                      #   tab: { path: "/kanban", position: "after:skills" },
│   │                      #   entry: "dist/index.js", css: "dist/style.css", api: "plugin_api.py" }
│   ├── plugin_api.py      # 1807 lines — the backend (verified in full)
│   └── dist/
│       ├── index.js       # board UI: drag-drop cards, comment threads, profiles
│       └── style.css
└── systemd/
    └── hermes-kanban-dispatcher.service
```

## What to steal from plugin_api.py (verified patterns, by line range)

| Lines (approx) | Pattern | Why it matters for us |
|---|---|---|
| 1-57 | Module docstring stating the design intent; thin wrappers around `hermes_cli.kanban_db` | "the same code paths the CLI and gateway use, so the surfaces cannot drift" — our backend wraps the conversations DB layer the same way |
| 49-57 | `_ws_upgrade_authorized` — delegate WS auth to the dashboard's canonical gate | Never invent auth; reuse `web_server_chat._ws_auth_ok` |
| 94-106 | `_board_conn` / `_with_board_pinned` context managers | Connection lifecycle + per-request board pinning (not process-global env) |
| 109-162 | Error-mapping helpers: `_require` (404), `_conflict` (409), `_map_errors` (type→status), `_errors_to_500` | The exact HTTP semantics both UIs depend on |
| 169 | `BOARD_COLUMNS: list[str]` — the single source of column order, kept in sync with the domain's VALID_STATUSES ("a status missing here gets mis-bucketed") | Our conversation states → columns mapping lives in ONE list with the same warning |
| 171 | `_CARD_SUMMARY_PREVIEW_CHARS = 200` | Card previews truncated; full text only from the detail route |
| 174-187 | `_task_dict` — serialization with derived `age` metrics | Cards carry derived fields so the UI can color stale items without client deltas |
| 283-362 | `GET /board` — one aggregate query per rollup (links, comments, progress), window functions for latest summaries, `coalesced_read` wrapper | The N+1-avoidance blueprint for `GET /board` of conversations |
| 344 | `_read_board = coalesced_read(get_board)` | `hermes_cli.web_read_coalescing.coalesced_read` collapses concurrent identical reads |
| 400-445 | `POST /tasks` — Pydantic body with explicit fields; dispatcher-presence warning included in the create response | Explicit bodies; operational warnings travel in the payload, not logs |
| 560-602 | `_STATUS_HANDLERS` — status verbs dispatched through the DOMAIN layer (`unblock_task`, `reopen_review_task`), never raw writes; `running` rejected from the dashboard | Our state transitions (`muta`, `riapri`, `chiudi`) go through a domain function, not a bare UPDATE |
| 626-643 | `_patch_status` — 400 rejected verb, 409 transition refused **naming the blocker** ("so the UI renders an actionable toast") | Error detail = what the user should do next |
| 730-780 | `_set_status_direct` — write txn + events + post-commit worker terminations, `recompute_ready` after reopen | Every state change writes an event row (audit trail) inside the same txn |
| 908-921 | `GET /workers/active` — open runs with live pids | Pattern for showing "agente attivo su questa chat" |
| 1041-1124 | `_run_estimate` — aux-LLM call with tolerant JSON extraction; **never raises** — errors become `{"ok": false, "reason"}` the UI renders inline | How to call a classifier (our Jev-style triage) from the backend safely |
| 1677-1807 | `WS /events` — `_EventTail`: per-socket connection on a single-thread executor, cursor semantics (None = current tail, explicit `since` replays), `asyncio.wait_for(ws.receive(), timeout=0.3)` race so idle sockets don't leak | The complete live-update recipe for the dashboard half |

## Manifest as shipped (verified)

```json
{
  "name": "kanban",
  "label": "Kanban",
  "description": "Multi-agent collaboration board — drag-drop cards across columns, read comment threads, see which profile is running what",
  "icon": "Package",
  "version": "1.0.0",
  "tab": { "path": "/kanban", "position": "after:skills" },
  "entry": "dist/index.js",
  "css": "dist/style.css",
  "api": "plugin_api.py"
}
```

## Its tests (the TDD reference)

- `/Users/crotti/.hermes/hermes-agent/tests/plugins/test_kanban_estimate.py` —
  the router-load + TestClient + tmp HERMES_HOME pattern (detailed in
  05-testing-tdd.md; full file read and mirrored in our template).
- `/Users/crotti/.hermes/hermes-agent/tests/plugins/test_kanban_read_admission.py` —
  same pattern for admission/authorization.

## Differences from hermes-whatsapp-chat (do NOT copy blindly)

| Aspect | kanban plugin | hermes-whatsapp-chat |
|---|---|---|
| Domain | `hermes_cli.kanban_db` (task engine) | conversations archive DB (owned by the WhatsApp pipeline) |
| Writes | full task lifecycle incl. spawning workers | read-mostly; only conversation-state transitions |
| Columns | task statuses (triage/todo/ready/running/blocked/review/done) | conversation states (NUOVA / IN_GESTIONE / IN_ATTESA / MUTATA / CHIUSA + fallback) |
| WS events | tails `task_events` | tails the conversations DB's event/row changes (or reuses broadcast_plugin_event) |
| Dispatcher | spawns agent workers | none (the board is a view, not an orchestrator) |

**The one design rule from the entity-automation skill that applies here:** if
the board ever creates kanban cards for follow-up workers, it's ONE card per
contact/entity kept open across its lifecycle — subsequent events are
`kanban_comment` on the same card, never new cards. See
[skills/hermes-entity-automation.md](skills/hermes-entity-automation.md).
