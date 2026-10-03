# Repository Guidelines

## Project Overview

Repository of `hermes-whatsapp-chat` v2, a Hermes plugin installed locally: a multi-number WhatsApp chat (desktop UI + web dashboard) with a kanban board, conversation rules and an automation/dispatcher layer, shipped as **one unified Hermes plugin** (desktop UI + web dashboard UI + Python FastAPI backend) plus an **independent WhatsApp channel** (`sidecar/`: vendored Baileys bridge, one per number, supervised by a LaunchAgent service; never Hermes' native WhatsApp). The plugin owns its SQLite DB, renders conversations, applies the rules, sends replies and runs automations. Hermes itself can operate the chat through a CLI (`scripts/wa.py`) and a skill (`hermes-skill/whatsapp-chat/`).

`README.md` is the user-facing presentation (install, usage, architecture). `docs/` is the original build kit (Hermes SDK notes, kanban case study, TDD notes); `docs/08-spec-wa-board.md` is the v1 spec and is **superseded** by this file and the code.

Every claim in `docs/` was checked against the local Hermes source at `/Users/crotti/.hermes/hermes-agent/`. When a doc and the Hermes source disagree, trust the source. The live skills under `~/.hermes/skills/` are authoritative over the copies in `docs/skills/`.

## Architecture & Data Flow

```
UI desktop: ctx.rest/ctx.socket        UI dashboard: SDK.fetchJSON / WebSocket
 plugin/desktop/plugin.js (ESM)         plugin/dashboard/dist/index.js (IIFE)
              └──────────────┬──────────────────────┘
                             ▼  /api/plugins/hermes-whatsapp-chat/
        plugin/dashboard/plugin_api.py  (thin routes + WS /events; exposes module attr `core`)
                             │ core = plugin/dashboard/wa_core/ package
                             ▼
        SQLite DB  <data>/wa_board.db   ◀── control plane: desired state, status, QR, heartbeat
                             ▲
        sidecar/wa_channel.py (LaunchAgent it.fl1.hermes-whatsapp-chat.channel, reconcile every 1 s)
          ├─ one Baileys bridge per account (own port + session dir)  ──▶ WhatsApp
          ├─ QR pairing subprocess (node bridge.js --pair-only --pair-json)
          └─ automations executor (2-thread pool) ──▶ hermes CLI / webhook / script
        scripts/wa.py (CLI, used by Hermes agents) ──▶ same core + DB
```

- **There are three surfaces and two different JS SDKs.** The desktop and dashboard halves share **only** the backend namespace `/api/plugins/<id>/`. Never mix their APIs.
  - Desktop: `import … from '@hermes/plugin-sdk'`. The default export is `{id, name, defaultEnabled, register(ctx)}`. Contributions go through `ctx.registerMany` (route page at `/wa-board`, palette command `wa-board.open`, status bar, etc.).
  - Dashboard: a plain `(function(){ 'use strict'; … })()` that uses `window.__HERMES_PLUGIN_SDK__` (`SDK.React`, `SDK.components`, `SDK.fetchJSON`, `SDK.utils`) and registers through `window.__HERMES_PLUGINS__.register(PLUGIN_ID, Component)`.
  - Backend: `plugin_api.py` exposes a module-level `router = APIRouter()`. The gateway mounts it at startup. It is imported **only if** the plugin is listed in `plugins.enabled` in `~/.hermes/config.yaml`; toggling it in the desktop panel is not enough.
- **Desktop = full UI** (Chats, Board, Settings incl. numbers/pairing, rules, notifications, board, hours, automations). **Dashboard = board + chat (thread, reply, drafts) + numbers status/QR**; settings and automations are desktop only (the dashboard shows a hint).
- **Live updates:** backend `WS /events` tails the `events` table (frames `{events, cursor}`; `?since=` cursor, none = start at tail; poll interval module global `_EVENT_POLL_SECONDS`). Desktop: `ctx.socket('/events', …)` invalidates React Query and raises native notifications per the notification settings. Dashboard: a `WebSocket` from `SDK.buildWsUrl(...)` bumps a refetch tick. Both halves **always** keep a polling fallback (5s), because `ctx.socket` is a no-op on OAuth remotes.
- **Auth for the WS:** `_ws_upgrade_authorized` (delegates to the dashboard's `_ws_auth_ok`, accepts when it is not importable, e.g. in tests); unauthorized sockets close with 1008.

### Backend package (`plugin/dashboard/wa_core/`)

`plugin_api.py` contains only request models, error mapping (`_session()` maps `WaError` to its HTTP status, anything else to 500) and routes. All logic is in the `wa_core` package, loaded **by path** (no `sys.path` changes) by `_load_core()` in `plugin_api.py`: it deletes any `hermes_whatsapp_chat_core*` entries from `sys.modules` and re-executes the package, so tests and plugin rescans always see current code. Inside `wa_core` use **relative imports only** (`from . import db`). Scripts and the sidecar load `plugin/dashboard/plugin_api.py` by path and use `api.core.<module>` (the repo `.venv` has fastapi).

| Module | Responsibility |
|---|---|
| `errors.py` | `WaError(Exception)` with `.status`: `NotFound` 404, `Conflict` 409, `Invalid` 400, `Unavailable` 503, `BadGateway` 502, `TooLarge` 413 |
| `db.py` | paths (`data_dir`, `db_path`), `SCHEMA`, v1→v2 migration, `connect()`, `write_txn()`, `jloads()` |
| `settings.py` | pydantic `Settings` model (single JSON row `global`), `get_settings`, `save_settings` (emits `settings.updated`) |
| `accounts.py` | accounts CRUD, desired state, restart, status/QR, service heartbeat/`service_info`, session/media paths |
| `bridge.py` | tiny HTTP client to one bridge port (`bridge_request`, raises `BridgeUnavailable`) |
| `conversations.py` | board, list, detail, state machine, mute/takeover/escalate/read/tags, search, timers (`run_timers`) |
| `outbound.py` | `send_text`, `send_media`, drafts (`create_draft`, `approve_draft`, `discard_draft`) |
| `ingest.py` | `ingest_event`: the rules engine for inbound/owner/history messages |
| `media.py` | media listing and `data:` URLs (path-prefix guarded) |
| `events.py` | `emit()`: inserts an `events` row and enqueues matching automation runs |
| `automations.py` | rule matching, run queue, executor (`process_due`), action types |
| `automations_api.py` | `router` with the automation routes, included by `plugin_api.py` |

### Multi-account model

- A conversation is keyed by `(account_id, chat_jid)`: the same contact on two numbers is two conversations. **Every API addresses a conversation by its integer `conversation_id`**; `chat_jid` is only an attribute.
- Account `kind` is `whatsapp` or `demo` (demo = `scripts/seed_demo.py`, JIDs `@demo.invalid`, can never be messaged). `desired` ∈ `running|stopped|logged_out|removed`; `history_mode` ∈ `off|recent|full`; optional `hermes_profile` is the default profile for that number's Hermes automations.
- New accounts get `session_dir = sessions/<id>` and the lowest free port ≥ 3017 (≠ 3000). Media dir `<data>/wa-media/<id>`, uploads `<data>/uploads/<account_id>/`.
- All timestamps are integer unix seconds.

### Schema v2 and migration

`PRAGMA user_version = 2`, created in `db.connect()` (WAL). Tables: `accounts`, `account_status`, `service_status`, `settings`, `conversations`, `messages`, `conversation_state_log`, `events`, `automation_rules`, `automation_runs` (+ indexes). The full DDL is `SCHEMA` in `wa_core/db.py`.

- `messages.author`: `contact`, `user` (sent from the UI), `phone` (typed on the linked phone), `auto_reply` (WhatsApp Business away/greeting echo), `agent:<profile|default>`, `rule:<id>`, `cli`. `status` ∈ `received|pending|sent|failed|draft|discarded`; `source` ∈ `live|history`. Media meta: `{"mediaType", "media": [{"path","mime","name","size"}]}`.
- Migration v1→v2 runs in `connect()` in one transaction when `user_version < 2`: a v1 `conversations` table (`chat_jid` primary key) becomes account 1 (`Main`, green, port 3017, `wa-session` dir) if real conversations or `wa-session/creds.json` exist, plus a `Demo` account for `@demo.invalid` chats; conversations, messages (in→author `contact`/status `received`, out→`user`/`sent`) and the state log are copied, old tables dropped. A fresh DB gets the schema directly with no accounts.
- Paths: data dir = `plugins.plugin_storage.plugin_data_dir('hermes-whatsapp-chat')` (guarded import) with fallback `${HERMES_HOME:-~/.hermes}/plugin-data/hermes-whatsapp-chat`; DB = `$WA_ARCHIVE_DB` or `<data>/wa_board.db`.

### Routes (prefix `/api/plugins/hermes-whatsapp-chat`)

Errors are `{"detail": str}`.

- Health/service: `GET /health` (never raises), `GET /service`.
- Accounts: `GET|POST /accounts`, `PATCH|DELETE /accounts/{id}` (`?delete_conversations=`), `POST /accounts/{id}/start|stop|restart|logout`.
- Settings: `GET|PUT /settings` (PUT replaces the whole validated object, 422 on invalid).
- Reading: `GET /board?account_id=&q=&include_closed=`, `GET /conversations?account_id=&state=&q=&unread_only=&limit=&offset=`, `GET /conversations/{id}`, `GET /conversations/{id}/messages?before_id=&limit=`, `GET /messages/{id}/media/{index}`, `GET /search?q=&account_id=`.
- Mutations: `POST /conversations/{id}/state|mute|takeover|handback|escalate|read`, `PUT /conversations/{id}/tags`, `POST /conversations/{id}/reply|reply-media|drafts`, `POST /messages/{id}/approve|discard`.
- Automations (`automations_api.router`): `GET|POST /automations`, `PUT|DELETE /automations/{id}`, `POST /automations/reorder`, `POST /automations/{id}/test` (dry run), `GET /automation-runs`, `POST /automation-runs/{id}/retry`.
- `WS /events`.

### Rules engine (`ingest.py`, `conversations.py`)

- **States:** `new`, `in_progress`, `waiting`, `muted` (snooze via `muted_until`), `closed`. `ALLOWED_TRANSITIONS` (`conversations.py`): new→{in_progress,muted,closed}, in_progress→{waiting,muted,closed}, waiting→{in_progress,muted,closed}, muted→{in_progress,new}, closed→{in_progress}. A manual invalid move returns **409** naming the current state and the allowed moves. Automatic rule transitions bypass the table (they are policy), set `previous_state`, and log actor `auto` with a reason.
- **Ingest:** groups are ignored; dedup by `(account_id, wa_id)`. Inbound on a new conversation → `new` (emits `conversation.created`); on `closed`/`waiting`/`muted` it follows `settings.rules` (`inbound_on_closed|waiting|muted`). Muted only wakes for substantive messages (not reactions/poll votes). An owner message (`fromOwner`) is author `phone`, or `auto_reply` when within `auto_reply_window_seconds` of the last inbound (no state change, unread untouched). Outbound on `new`/`in_progress` follows `outbound_on_active` (default → `waiting`).
- **History source:** conversations created by history import start `closed`; history messages never trigger rules, unread, notifications, events or automations.
- **Timers:** `run_timers` (called by the sidecar every 30 s): expired mute → `new`, `waiting` idle longer than `auto_close_waiting_days` → `closed`.
- **Take over / Escalate:** "Take over" sets `in_progress` and `agent_active=0` (reversible via "Hand back"); "Escalate" sets `priority=2` (reversible). `agent_active=0` makes automations skip reply-producing actions.
- **Outbound:** `send_text` stores a `pending` message, calls the bridge `POST /send`, then marks `sent`/`failed` (failed stays visible) and applies the outbound rule. Demo accounts and non-WhatsApp JIDs are rejected; unpaired or stopped accounts raise 503. `send_media` is capped at 15 MB. Drafts are `status='draft'`; approving sends.

### Settings (`wa_core/settings.py`)

One JSON row, key `global`, validated by the pydantic `Settings` model with defaults: `rules` (the five inbound/outbound policies above, `auto_reply_window_seconds`, `auto_close_waiting_days`), `notifications` (`new_conversation`, `every_inbound`, `escalation`, `quiet_hours`), `board` (`urgency_hours`, `mute_presets_hours`, `drop_mute_hours`, `closed_limit`), `hours` (timezone + weekly business hours), `automations` (`enabled`, `max_runs_per_conversation_per_hour`, `history_messages_in_prompt`), `media` (`max_upload_mb`). The model is the single source of truth for defaults and bounds.

### Events and automations

- `events.emit(conn, type, ...)` **must be called inside the caller's `write_txn`**; every core mutation emits inside its own transaction. Types: `conversation.created`, `conversation.state_changed`, `conversation.updated`, `message.in`, `message.out`, `message.draft`, `message.status`, `account.status`, `automation.run`, `settings.updated`.
- Only `message.in`, `message.out`, `conversation.created`, `conversation.state_changed` trigger automations, never for history messages and never for messages authored `agent:*` / `rule:*` (loop guard). Matching only inserts `automation_runs` rows (a broken rule never rolls back the original change).
- Execution (`automations.process_due`) claims queued runs in a short transaction and performs the action **outside** any transaction. Retries: 3 attempts, backoff 30 s / 120 s. Rate limit per conversation per hour from settings.
- Rule: optional `account_id`, `event_types`, `conditions` (`states`, `agent_active`, `business_hours`, `text_contains`, `text_regex`, `tags_any`, `first_message`, `directions`), `action`, `reply_mode` ∈ `draft|send|none`, `stop_after_match`. Actions: `hermes` (`hermes [-p profile] chat -Q -q <prompt> --source whatsapp-chat [-s skill]`; profile falls back to the account's `hermes_profile`; binary resolved via `WA_HERMES_BIN`, `PATH`, then known locations), `webhook` (HMAC `X-WA-Signature`), `script`, `reply`, `set_state`, `add_tags`, `escalate`, `takeover`. Prompt templates: `{contact_name} {phone} {text} {account_label} {state} {conversation_id} {history} {wa_cli}`.

### Sidecar (`sidecar/wa_channel.py`)

One supervisor loop (every 1 s) per LaunchAgent `it.fl1.hermes-whatsapp-chat.channel`: writes the service heartbeat, reconciles each `kind='whatsapp'` account by `desired` (running: not paired → pairing subprocess, QR rendered with `segno` into `account_status.qr_svg`; paired → bridge process `node bridge.js --session <path> --mode bot --port <port>`; stopped → stop; logged_out → stop + delete session; removed → stop + delete + `finalize_removed`), restarts on `restart_requested_at`, polls each bridge's `/messages` and `/history` into `core.ingest`, reads `/health`, runs timers every 30 s and `automations.process_due` on a 2-thread pool so ingest never blocks. The UI treats a heartbeat older than 15 s as "service not running". The control plane is the DB: **no HTTP port for control**.

Subcommands: `run`, `install`, `uninstall`, `status` (service + accounts), `install-skill` (copies `hermes-skill/whatsapp-chat/` to `~/.hermes/skills/whatsapp-chat/`). Pairing is in the UI; there is no `pair` subcommand.

### Bridge patches policy

`sidecar/whatsapp-bridge/` is copied verbatim from Hermes (`scripts/whatsapp-bridge`) and **never edited by hand**. Local changes are versioned patches in `sidecar/patches/*.patch` (currently `0001-history-sync.patch`: `WHATSAPP_SYNC_HISTORY=off|recent|full` and `GET /history`). Refresh with `sidecar/update_bridge.sh [HERMES_CHECKOUT]` (stages in a temp dir, applies patches in order, fails without touching the vendored copy if one does not apply, rewrites `UPSTREAM`), then `npm ci --prefix sidecar/whatsapp-bridge`. Keep patches minimal.

### CLI and skill

`scripts/wa.py` (run with `<repo>/.venv/bin/python`): `list [--state S] [--account ID] [--unread] [--limit N] [--json]`, `show ID`, `send ID TEXT`, `draft ID TEXT`, `state ID STATE [--reason R]`, `tag ID +a -b`, `takeover ID`, `handback ID`, `search QUERY [--account ID]`. It loads `plugin_api.py` by path and calls `core` directly. Author of what it writes: `agent:$HERMES_PROFILE` if set, else `cli`. Exit 0 ok, 1 error (message on stderr). The skill `hermes-skill/whatsapp-chat/SKILL.md` documents it (absolute paths, draft-first rule); keep it in sync with the CLI.

### Hermes-update safety

Use only public surfaces: the plugin router, the two UI SDKs, the `hermes` CLI (`hermes [-p PROFILE] chat -Q -q PROMPT --source whatsapp-chat`) and HTTP. **Never edit Hermes core** and never import Hermes internals except the existing guarded `_ws_upgrade_authorized` and `plugins.plugin_storage` (both with `ImportError` fallbacks). No `__init__.py` at the plugin root. `plugin/plugin.yaml` sets `python_runtime: external`.

## Key Directories

| Path | Purpose |
|---|---|
| `plugin/` | The unified package, installed as `~/.hermes/plugins/hermes-whatsapp-chat/`: `plugin.yaml`, `dashboard/{manifest.json, plugin_api.py, wa_core/, dist/{index.js,style.css}}`, `desktop/plugin.js` |
| `sidecar/` | Independent WhatsApp channel: `wa_channel.py`, `whatsapp-bridge/` (vendored Baileys bridge, see `UPSTREAM`), `patches/`, `update_bridge.sh` |
| `scripts/wa.py` | CLI for Hermes agents and humans |
| `scripts/seed_demo.py` | Demo data in a `kind='demo'` account (`--reset` / `--remove`); demo chats are `@demo.invalid` |
| `hermes-skill/whatsapp-chat/` | Hermes skill teaching agents the CLI (installed by `wa_channel.py install-skill`) |
| `.agents/skills/` | Project skills (Hermes plugin development, desktop plugins, Hermes agent) |
| `tests/` | pytest suite: `test_plugin_api.py`, `test_automations.py` |
| `docs/` | Original build kit (01–07 background; 08 spec is superseded) and `docs/skills/`, `docs/examples/` |

## Development Commands

```bash
# Dev env (Python 3.11, matches the Hermes venv). NEVER pip install into the Hermes venv.
uv sync --python 3.11
npm ci --prefix sidecar/whatsapp-bridge

# Tests / lint
uv run pytest -q
uv run ruff check .
uv run ty check plugin scripts sidecar tests
pnpm lint && pnpm format:check

# Validate / install / enable
hermes plugins validate .                          # run from plugin/
# Dev alias: a REAL folder of symlinks. Never symlink the whole plugin dir: the desktop app
# lists ~/.hermes/plugins/ with isDirectory() (false for symlinks) and would never copy the
# desktop half (Plugins page stuck on "copying…").
P=~/.hermes/plugins/hermes-whatsapp-chat; mkdir -p $P/desktop
ln -s "$PWD/plugin/plugin.yaml" $P/plugin.yaml; ln -s "$PWD/plugin/dashboard" $P/dashboard
ln -s "$PWD/plugin/desktop/plugin.js" $P/desktop/plugin.js
hermes plugins enable hermes-whatsapp-chat
hermes gateway restart                             # backend routes mount only at startup

# Channel service (LaunchAgent)
uv run python sidecar/wa_channel.py install        # start now and at login
uv run python sidecar/wa_channel.py status         # service heartbeat + per-account state
uv run python sidecar/wa_channel.py install-skill
tail -f ~/.hermes/plugin-data/hermes-whatsapp-chat/logs/channel.log

# Live check against the running backend/service
curl -s http://127.0.0.1:<dashboard-port>/api/plugins/hermes-whatsapp-chat/health   # needs the dashboard session token if enabled
.venv/bin/python scripts/wa.py list
uv run python scripts/seed_demo.py --reset         # demo data; --remove to delete

# Reload UI halves
#   dashboard: restart, or GET /api/dashboard/plugins/rescan
#   desktop:   Capabilities → Plugins toggle; ⌘K → "Reload desktop plugins"

# Debug
hermes logs gui -f
tail -f ~/.hermes/logs/errors.log                  # "Failed to load plugin <id> API routes"
```

The JS halves are hand-written files that load as-is; there is no build step.

## Code Conventions & Common Patterns

**Naming**
- The plugin id `hermes-whatsapp-chat` must be identical in four places: the folder name, the manifest `name`, the `plugin.yaml` `name`, and the desktop `id`.
- Dashboard tab path: `/wa-board`. Palette id: `wa-board.open`.
- Event wire name: `plugin.<id>.<dotted.event>`.
- Desktop React Query keys are built by `useApi`: `[ID, key, path]`; `refresh()` invalidates everything under `[ID]`.
- UI labels, state ids, tags and demo data are in English everywhere (`STATE_LABELS`); dates use the viewer's locale. The desktop app bundles no Italian locale, so there is no translation layer.

**Desktop `plugin.js`**
- It loads **uncompiled**. Use `jsx()`/`jsxs()` from `react/jsx-runtime`, **never JSX syntax**.
- Only three import specifiers resolve: `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`. Any other import breaks loading.
- No top-level `await`. Module evaluation has a 10s deadline.
- Use `ctx.setTimeout`, `ctx.setInterval`, `ctx.addEventListener`, and `ctx.onDispose`. A bare `window.*` timer or listener leaks across hot-reloads.
- Keep the `ctx` from `register()` in a module-level `ctxRef`. Read atoms with `.get()` inside handlers, not from render closures.
- Export pure helpers (e.g. `fmtAge`, `inQuietHours`) so tests can target them.

**Dashboard bundle**
- An ES5-style IIFE: `var`/`function`, with `h = React.createElement`.
- State lives in a small listener store plus polling. Clean up inside the `useEffect` return.
- Conversations are addressed by integer id in URLs (`/conversations/<id>/…`).

**Styling**
- Theme variables only (`var(--ui-*)`). **Never hardcode colors.**

**Backend**
- Routes stay thin: parse the request, call `core.<module>`, return. Logic and SQL belong in `wa_core`.
- Connect with `closing(core.db.connect())` (`sqlite3.Row`, `isolation_level=None`); wrap writes in `db.write_txn(conn)`.
- Raise the `wa_core.errors` classes for domain failures; never raise `HTTPException` from `wa_core`.
- Write each mutation, its audit row (`conversation_state_log` where relevant) and its `events.emit` inside **one transaction**. `actor` is set by the server (default `'user'`) and never taken from the client.
- `BOARD_COLUMNS` is the single source of truth for columns; `FALLBACK_COLUMN` catches unknown states so they are never dropped.
- Build `/board` from aggregate queries (no N+1). Truncate previews to 200 chars. Compute derived fields such as `age_seconds`.
- Pydantic request bodies declare explicit fields only.
- Do network/subprocess work (bridge calls, hermes, webhooks) outside open write transactions.
- WebSocket handlers need a single-thread executor per socket (SQLite thread affinity) and reuse `_ws_upgrade_authorized`.

**Degradation**
- Both UI halves must still render a clear error state ("Backend unreachable") when the backend is off or `/api/plugins/<id>/` can't be reached (e.g. a remote gateway); with data already loaded they keep it and show "Data may be stale". When the sidecar heartbeat is stale they show a "service not running" banner with the install command.

**Copying from the kanban plugin** (`docs/06-case-study-kanban.md`)
- Reuse its auth delegation, error mapping, aggregated `/board`, and `_EventTail` idea. Do **not** copy its worker dispatch or task engine.

## Important Files

| File | Why it matters |
|---|---|
| `README.md` | User-facing presentation: install, usage, architecture, data locations |
| `plugin/dashboard/plugin_api.py` | Routes, request models, loader for `wa_core`, `WS /events` |
| `plugin/dashboard/wa_core/` | All backend logic (see the module table) |
| `sidecar/wa_channel.py` | Supervisor, pairing, ingest loop, LaunchAgent install, `status` |
| `sidecar/update_bridge.sh`, `sidecar/patches/` | Vendored bridge refresh and local patches |
| `scripts/wa.py`, `hermes-skill/whatsapp-chat/SKILL.md` | CLI and skill for Hermes agents |
| `tests/test_plugin_api.py`, `tests/test_automations.py` | pytest suites (real router + tmp SQLite) |
| `plugin/desktop/plugin.js` / `plugin/dashboard/dist/index.js` | The two UI halves |
| `plugin/dashboard/manifest.json` | Required fields: `name`, `tab.path`, `entry`. `api` points to `plugin_api.py` |
| `plugin/plugin.yaml` | `name`, `version`, `description`, `python_runtime: external` |
| `/Users/crotti/.hermes/hermes-agent/plugins/kanban/` | Live reference plugin |
| `/Users/crotti/.hermes/hermes-agent/website/docs/developer-guide/desktop-plugin-sdk.md` | Authoritative desktop SDK doc |

## Runtime/Tooling Preferences

- **Python 3.11** in a local `.venv`. The Hermes runtime venv (`~/.hermes/hermes-agent/venv`) has no pytest and must not be modified. Runtime deps of the repo env: `fastapi`, `pydantic`, `segno`.
- **Node:** the sidecar needs Node ≥ 20 (Hermes ships 22 at `~/.hermes/node/bin/node`); `WA_NODE` overrides the binary, otherwise `node` from `PATH`.
- **Dev tooling lives at the repo root only** (`pyproject.toml`, `package.json`, eslint/prettier configs): never inside `plugin/`. JS: `pnpm lint`, `pnpm format:check`. Python: `uv run pytest`, `uv run ruff check .`, `uv run ty check plugin scripts sidecar tests`. No bundler: the JS halves load as-is. `sidecar/whatsapp-bridge/` uses npm (`npm ci --prefix sidecar/whatsapp-bridge`).
- **Never edit `~/.hermes/config.yaml` by hand.** Use `hermes config set KEY VAL`.
- **Edit the package source, not the installed copy.** `hermes plugins update` overwrites hand edits in `~/.hermes/desktop-plugins/`.
- Useful env vars:
  - `HERMES_HOME` (default `~/.hermes`)
  - `HERMES_ENABLE_PROJECT_PLUGINS=true` (project-local plugins, dev only)
  - `WA_ARCHIVE_DB` (default: `<data>/wa_board.db`)
  - `WA_NODE` (Node binary for the sidecar)
  - `WA_HERMES_BIN` (`hermes` binary used by Hermes automations)
  - `HERMES_PROFILE` (author of CLI-written messages: `agent:<profile>`)
  - `install` stores `WA_NODE`, `WA_ARCHIVE_DB` and `HERMES_HOME` in the LaunchAgent environment; re-run it after changing them.

## Testing & QA

- **Framework:** pytest with FastAPI `TestClient` (requires `httpx`). Files: `tests/test_plugin_api.py` (routes, rules engine, accounts, outbound, migration, settings, events) and `tests/test_automations.py` (matching, executor, actions, retries, loop guard).
- **Pattern:**
  1. Load the **real** `plugin/dashboard/plugin_api.py` with `importlib.util.spec_from_file_location` (the `wa_core` package loads fresh through it).
  2. Mount `mod.router` in a bare `FastAPI()` with prefix `/api/plugins/hermes-whatsapp-chat`.
  3. Seed with small helpers or `scripts/seed_demo.py`.
  4. Point the plugin at a temp DB with `monkeypatch.setenv("WA_ARCHIVE_DB", …)`; the faked boundaries are the bridge (`core.bridge.bridge_request`) and external actions (the `hermes` subprocess, webhook HTTP, scripts).
- **TDD, red before green, vertical slices:** one test, then just enough code to pass, then repeat. The tracer bullet is `GET /health`.
- **Seams:** the backend HTTP API, the `wa_core` functions over a real tmp SQLite, desktop pure functions.
- **Mocking:** only at external boundaries (bridge, LLM/hermes, network), never internal collaborators. Assert through the HTTP interface; query the DB only to seed data or check persisted effects.
- **Naming:** `test_<behavior>`, e.g. `test_invalid_transition_409_names_allowed`. Group tests with comments like `# --- GET /board ---`.
- **Cover for every change:** state transitions (409 on invalid), rules per setting, history never triggers rules/automations, loop guard, `agent_active=0` skips, drafts (create/approve/discard), per-account isolation, v1→v2 migration, `/board` without N+1, empty DB returns 200.
- **Out of scope:** UI pixel rendering, Hermes itself, the real WhatsApp bridge.
- **Done** means pytest, ruff, ty and the JS lint are green, `wa_channel.py status` shows the service running and the numbers connected, the board shows data on the Mac, notifications work, and the UI degrades gracefully.
