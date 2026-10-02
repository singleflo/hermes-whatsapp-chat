# Repository Guidelines

## Project Overview

Repository of `hermes-whatsapp-chat`, a Hermes plugin installed locally: a kanban board of WhatsApp conversations, shipped as **one unified Hermes plugin** (desktop UI + web dashboard UI + Python FastAPI backend) plus an **independent WhatsApp channel** (`sidecar/`: vendored Baileys bridge + LaunchAgent service on a dedicated number, never Hermes' native WhatsApp). The plugin owns its SQLite DB, renders conversations, writes state transitions and sends manual replies.

The code in `plugin/` and `tests/` started from verified skeletons; complete it following `docs/08-spec-wa-board.md`.

Every claim in `docs/` was checked against the local Hermes source at `/Users/crotti/.hermes/hermes-agent/`. When a doc and the Hermes source disagree, trust the source. The same applies to `docs/skills/`: they are copies, and the live skills under `~/.hermes/skills/` are authoritative.

## Architecture & Data Flow

```
Baileys bridge (sidecar/whatsapp-bridge, port 3017, own session) ──poll──▶ sidecar/wa_channel.py
                                          │ ingest_event
                                          ▼
                               SQLite DB  (plugin-data/hermes-whatsapp-chat/wa_board.db)
                                          │ read (+ state writes)
                                          ▼
              plugin/dashboard/plugin_api.py  → /api/plugins/hermes-whatsapp-chat/
                     ▲                                   ▲
   desktop: ctx.rest('/board')            dashboard: SDK.fetchJSON('/api/plugins/<id>/board')
   desktop/plugin.js (Electron, ESM)      dashboard/dist/index.js (browser, IIFE)
```

- **There are three surfaces and two different JS SDKs.** The desktop and dashboard halves share **only** the backend namespace `/api/plugins/<id>/`. Never mix their APIs.
  - Desktop: `import … from '@hermes/plugin-sdk'`. The default export is `{id, name, defaultEnabled, register(ctx)}`. Contributions go through `ctx.registerMany` into `ROUTES_AREA`, `SIDEBAR_NAV_AREA`, `PALETTE_AREA`, and `STATUSBAR_AREAS`.
  - Dashboard: a plain `(function(){ 'use strict'; … })()` that uses `window.__HERMES_PLUGIN_SDK__` (`SDK.React`, `SDK.components`, `SDK.fetchJSON`, `SDK.utils`) and registers through `window.__HERMES_PLUGINS__.register(PLUGIN_ID, Component)`.
  - Backend: `plugin_api.py` exposes a module-level `router = APIRouter()`. The gateway mounts it at startup. It is imported **only if** the plugin is listed in `plugins.enabled` in `~/.hermes/config.yaml`; toggling it in the desktop panel is not enough.
- **Live updates:**
  - Backend `WS /events` tails `conversation_state_log` and `messages`; frames are `{events, cursor, message_cursor}`.
  - Desktop: `ctx.socket('/events', …)` invalidates React Query and raises a native notification for each new conversation.
  - Dashboard: a `WebSocket` from `SDK.buildWsUrl(...)` bumps a refetch tick.
  - Both halves **always** keep a polling fallback (5s), because `ctx.socket` is a no-op on OAuth remotes.
- **Routes:** `GET /board?include_closed=&q=`, `GET /chats/{jid}`, `POST /chats/{jid}/{state|mute|takeover|handback|escalate|read|reply}`, `GET /stats` (includes `channel`), `GET /health`, `WS /events`. `docs/08-spec-wa-board.md` is the original spec; where it uses the Italian state names, the English ids above supersede it.
- **State machine:** states are `new`, `in_progress`, `waiting`, `muted` (snooze via `muted_until`), `closed`. `ALLOWED_TRANSITIONS` is in `plugin_api.py`; an invalid move returns **409** naming the current state and the allowed moves. `closed → in_progress` happens only manually. An expired mute reopens as `new`; an inbound message to a muted chat reopens it as `new`.
- **Take over / Escalate:** "Take over" sets `in_progress` and `agent_active=false` (reversible via "Hand back to agent"); "Escalate" sets `priority=2` (reversible). Nothing reads `agent_active` yet.
- **Data contract:** the plugin owns the schema (`conversations`, `messages`, `conversation_state_log`). The WhatsApp channel (`sidecar/wa_channel.py`) writes messages through `ingest_event`; manual replies go out via `POST /chats/{jid}/reply` → bridge `/send` on port 3017. Demo chats use `@demo.invalid` and can never be messaged.

## Key Directories

| Path | Purpose |
|---|---|
| `plugin/` | The unified package, installed as `~/.hermes/plugins/hermes-whatsapp-chat/`: `plugin.yaml`, `dashboard/{manifest.json, plugin_api.py, dist/index.js}`, `desktop/plugin.js`. Add `__init__.py` only if `register(ctx)` is needed; `dist/style.css` only if the manifest `css` is used |
| `sidecar/` | Independent WhatsApp channel: `wa_channel.py` (`pair` / `run` / `install` / `uninstall`; LaunchAgent `it.fl1.hermes-whatsapp-chat.channel`) and `whatsapp-bridge/` (unmodified Baileys bridge copied from Hermes, see `UPSTREAM`) |
| `scripts/seed_demo.py` | Demo data (`--reset` / `--remove`); demo chats are `@demo.invalid` |
| `.agents/skills/` | Project skills (Hermes plugin development, desktop plugins, Hermes agent) |
| `tests/` | pytest suite (`tests/test_plugin_api.py`) |
| `docs/01-…08-*.md` | Read in order. 01–05 and 07 explain how to build it, 06 is the reference implementation (Hermes kanban plugin), 08 is the functional spec |
| `docs/skills/` | Copies of the agent skills used to build the plugin (desktop plugins, webhooks, entity automation, TDD) |
| `docs/examples/desktop-plugin-minimal.js` | Official minimal desktop skill template (reference only, not installed) |

## Development Commands

```bash
# Dev env (Python 3.11, matches the Hermes venv). NEVER pip install into the Hermes venv.
uv sync --python 3.11

# Tests
uv run pytest -q

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
- React Query keys: `[ID, 'board']` and `[ID, 'stats']`.
- UI labels, state ids, tags and demo data are in English everywhere (`STATE_LABELS`); dates use the viewer's locale. The desktop app bundles no Italian locale, so there is no translation layer.

**Desktop `plugin.js`**
- It loads **uncompiled**. Use `jsx()`/`jsxs()` from `react/jsx-runtime`, **never JSX syntax**.
- Only three import specifiers resolve: `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`. Any other import breaks loading.
- No top-level `await`. Module evaluation has a 10s deadline.
- Use `ctx.setTimeout`, `ctx.setInterval`, `ctx.addEventListener`, and `ctx.onDispose`. A bare `window.*` timer or listener leaks across hot-reloads.
- Keep the `ctx` from `register()` in a module-level `ctxRef`. Read atoms with `.get()` inside handlers, not from render closures.
- Export pure helpers (e.g. `STATE_ORDER`, `urgencyColor`) so tests can target them.

**Dashboard bundle**
- An ES5-style IIFE: `var`/`function`, with `h = React.createElement`.
- State lives in a small listener store (`listeners`/`emit`/`refresh`) plus polling. Clean up inside the `useEffect` return.
- Encode path params with `encodeURIComponent(jid)`.

**Styling**
- Theme variables only (`var(--ui-*)`). **Never hardcode colors.**

**Backend `plugin_api.py`**
- The plugin owns its DB: `WA_ARCHIVE_DB` if set, else `<hermes home>/plugin-data/hermes-whatsapp-chat/wa_board.db`. `SCHEMA` is created on every connect.
- Connect with `closing(_conn())` and `sqlite3.Row`.
- Error helpers: `_not_found`, `_conflict`, `_map_errors(status, *types)`, `_errors_to_500(prefix)`.
- Write the state change **and** its audit row in `conversation_state_log` inside **one transaction**. `actor` is set by the server (default `'user'`) and never taken from the client.
- `BOARD_COLUMNS` is the single source of truth. `FALLBACK_COLUMN` catches unknown states so they are never dropped.
- Build `/board` from aggregate queries (no N+1). Truncate previews to 200 chars. Compute derived fields such as `age_seconds`.
- Pydantic request bodies declare explicit fields only.
- WebSocket handlers need a single-thread executor per socket (SQLite thread affinity) and should reuse `_ws_upgrade_authorized`.

**Degradation**
- Both UI halves must still render a clear error state ("Backend unreachable") when the backend is off or `/api/plugins/<id>/` can't be reached (e.g. a remote gateway); with data already loaded they keep it and show "Data may be stale".

**Copying from the kanban plugin** (`docs/06-case-study-kanban.md`)
- Reuse its auth delegation, error mapping, the aggregated `/board`, and `_EventTail`.
- Do **not** copy its worker dispatch or task engine. wa-board is read-mostly.

## Important Files

| File | Why it matters |
|---|---|
| `README.md` | Reading order and the 7 golden rules |
| `docs/08-spec-wa-board.md` | Source of truth for what to build: columns, transitions, card payload, routes, DoD |
| `docs/07-environment-and-paths.md` | Absolute paths, dev loop, gotchas |
| `plugin/dashboard/plugin_api.py` | Backend (routes, transition guard, error helpers) |
| `tests/test_plugin_api.py` | pytest suite (real router + seeded tmp SQLite) |
| `plugin/desktop/plugin.js` / `plugin/dashboard/dist/index.js` | The two UI halves |
| `plugin/dashboard/manifest.json` | Required fields: `name`, `tab.path`, `entry`. `api` points to `plugin_api.py` |
| `plugin/plugin.yaml` | `name`, `version`, `description`, optional `requires_env` |
| `/Users/crotti/.hermes/hermes-agent/plugins/kanban/` | Live reference plugin |
| `/Users/crotti/.hermes/hermes-agent/website/docs/developer-guide/desktop-plugin-sdk.md` | Authoritative desktop SDK doc |

## Runtime/Tooling Preferences

- **Python 3.11** in a local `.venv`. The Hermes runtime venv (`~/.hermes/hermes-agent/venv`) has no pytest and must not be modified.
- **Dev tooling lives at the repo root only** (`pyproject.toml`, `package.json`, eslint/prettier configs): never inside `plugin/`. JS: `pnpm lint`, `pnpm format:check`. Python: `uv run pytest`, `uv run ruff check .`, `uv run ty check plugin scripts sidecar tests`. No bundler: the JS halves load as-is. `sidecar/whatsapp-bridge/` is vendored from Hermes and uses npm (`npm ci --prefix sidecar/whatsapp-bridge`).
- **Never edit `~/.hermes/config.yaml` by hand.** Use `hermes config set KEY VAL`.
- **Edit the package source, not the installed copy.** `hermes plugins update` overwrites hand edits in `~/.hermes/desktop-plugins/`.
- Useful env vars:
  - `HERMES_HOME` (default `~/.hermes`)
  - `HERMES_ENABLE_PROJECT_PLUGINS=true` (project-local plugins, dev only)
  - `WA_ARCHIVE_DB` (default: `<hermes home>/plugin-data/hermes-whatsapp-chat/wa_board.db`)
  - `WA_BRIDGE_PORT` (default 3017)
  - `WA_NODE` (Node binary for the sidecar)

## Testing & QA

- **Framework:** pytest with FastAPI `TestClient` (requires `httpx`).
- **Pattern:**
  1. Load the **real** `plugin/dashboard/plugin_api.py` with `importlib.util.spec_from_file_location`.
  2. Mount `mod.router` in a bare `FastAPI()` with prefix `/api/plugins/hermes-whatsapp-chat`.
  3. Seed with small helpers (`add_conv`, `add_msg`) or `scripts/seed_demo.py`.
  4. Point the plugin at it with `monkeypatch.setenv("WA_ARCHIVE_DB", …)`; the WhatsApp bridge is the only faked boundary (`_bridge_request`).
- **TDD, red before green, vertical slices:** one test, then just enough code to pass, then repeat. The tracer bullet is `GET /health`.
- **Seams:**
  - the backend HTTP API
  - the DB query layer (real tmp SQLite)
  - desktop pure functions
- **Mocking:** only at external boundaries (e.g. LLM calls), never internal collaborators. Assert through the HTTP interface; query the DB only to seed data.
- **Naming:** `test_<behavior>`, e.g. `test_invalid_transition_409_names_allowed`. Group tests with comments like `# --- GET /board ---`.
- **Required cases** (05 + 08):
  - unknown state goes to the fallback column
  - empty DB returns 200 with empty columns
  - unknown jid returns 404
  - invalid transition returns 409
  - `mutata` sets `muted_until`
  - card payload: preview ≤200 chars, age, unread
  - `/stats` counts
  - `/health`
  - no N+1 on `/board`
  - p95 <300ms on a seeded DB of 400 conversations
- **Out of scope:** UI pixel rendering, Hermes itself, the WhatsApp bridge.
- **Done** means pytest is green, the board shows seeded data on the Mac, the desktop status chip and notifications work, and the UI degrades gracefully.
