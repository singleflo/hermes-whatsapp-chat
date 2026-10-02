# hermes-whatsapp-chat — Hermes Plugin Dev Kit

**Purpose:** complete, verified documentation package for building the
`hermes-whatsapp-chat` plugin ex novo — a kanban-style board of WhatsApp
conversations, built as a unified Hermes plugin (desktop UI + web dashboard UI +
Python backend), fed by the WhatsApp bridge / webhook pipeline.

**Consumer:** OMP (the building agent). Self-contained: read the docs, copy the
templates, follow the absolute paths. No assumptions — every technical claim in
this kit was verified against the local Hermes source tree
(`/Users/crotti/.hermes/hermes-agent/`) and the official docs
(hermes-agent.nousresearch.com) on 2026-09-29.

**Owner:** Roberto Crotti (FL1/Persevida). Planned GitHub repo: `crottolo/hermes-whatsapp-chat`.

---

## Reading order

| # | File | What it covers |
|---|------|----------------|
| 1 | [01-architecture.md](docs/01-architecture.md) | The 3 plugin surfaces, unified package layout, delivery modes |
| 2 | [02-desktop-plugin.md](docs/02-desktop-plugin.md) | Desktop app SDK (`@hermes/plugin-sdk`) — full verified reference |
| 3 | [03-dashboard-plugin.md](docs/03-dashboard-plugin.md) | Web dashboard plugin (manifest, `window.__HERMES_PLUGIN_SDK__`, slots) |
| 4 | [04-python-backend.md](docs/04-python-backend.md) | `plugin.yaml`, `register(ctx)`, `plugin_api.py` FastAPI, event push |
| 5 | [05-testing-tdd.md](docs/05-testing-tdd.md) | TDD: the proven test pattern from Hermes' own kanban plugin tests |
| 6 | [06-case-study-kanban.md](docs/06-case-study-kanban.md) | The bundled kanban dashboard plugin as the primary reference |
| 7 | [07-environment-and-paths.md](docs/07-environment-and-paths.md) | Absolute paths, dev loop, reload, validation commands |
| 8 | [08-spec-wa-board.md](docs/08-spec-wa-board.md) | Functional spec: the conversations board (what we converged on) |

## Repo layout

- [plugin/](plugin/) — the unified Hermes package (copy to `~/.hermes/plugins/hermes-whatsapp-chat/` to install)
  - [plugin.yaml](plugin/plugin.yaml) — agent-half manifest
  - [dashboard/manifest.json](plugin/dashboard/manifest.json) — dashboard manifest
  - [dashboard/plugin_api.py](plugin/dashboard/plugin_api.py) — shared FastAPI backend (modeled on the bundled kanban plugin)
  - [dashboard/dist/index.js](plugin/dashboard/dist/index.js) — dashboard UI (IIFE)
  - [desktop/plugin.js](plugin/desktop/plugin.js) — desktop UI (ESM, uncompiled)
- [tests/test_plugin_api.py](tests/test_plugin_api.py) — pytest/TDD suite (modeled on Hermes' own tests)
- [docs/](docs/) — this documentation; [docs/examples/desktop-plugin-minimal.js](docs/examples/desktop-plugin-minimal.js) is the official desktop skill template

## Skills (copies of the agent skills used to build this kit)

| Skill file | Origin (absolute path of the live skill) |
|---|---|
| [docs/skills/hermes-desktop-plugins.md](docs/skills/hermes-desktop-plugins.md) | `/Users/crotti/.hermes/skills/hermes-desktop-plugins/SKILL.md` |
| [docs/skills/hermes-entity-automation.md](docs/skills/hermes-entity-automation.md) | `/Users/crotti/.hermes/skills/business/hermes-entity-automation/SKILL.md` |
| [docs/skills/hermes-webhooks.md](docs/skills/hermes-webhooks.md) | `/Users/crotti/.hermes/skills/autonomous-ai-agents/hermes-agent/references/webhooks.md` |
| [docs/skills/tdd.md](docs/skills/tdd.md), [tdd-tests.md](docs/skills/tdd-tests.md), [tdd-mocking.md](docs/skills/tdd-mocking.md) | `/Users/crotti/.agents/skills/tdd/` |

## Golden rules (read first, always)

1. **Desktop and dashboard are DIFFERENT SDKs.** Desktop = `@hermes/plugin-sdk`
   ESM import in `desktop-plugins/<id>/plugin.js`. Dashboard = IIFE bundle +
   `manifest.json` in `plugins/<id>/dashboard/`, global `window.__HERMES_PLUGIN_SDK__`.
   They share ONLY the `plugin_api.py` backend namespace (`/api/plugins/<id>/`).
2. **Desktop plugins load uncompiled** — `jsx()` calls, never JSX syntax. Only 3
   importable specifiers: `@hermes/plugin-sdk`, `react`, `react/jsx-runtime`.
3. **The Python backend is gated**: `plugin_api.py` is imported ONLY when the
   plugin is in `plugins.enabled` in `config.yaml`. Toggling it in the Desktop
   Plugins panel alone does not import Python.
4. **Never hardcode colors** — theme vars (`var(--ui-*)`) only.
5. **No session continuity in kanban workers** — a worker is a fresh process per
   spawn; continuity is the comment thread. (Relevant if the board dispatches follow-ups.)
6. **Test against the real router**: load `plugin_api.py` via
   `importlib.util.spec_from_file_location`, mount in a bare FastAPI app, use
   `TestClient`. Pattern in [05-testing-tdd.md](docs/05-testing-tdd.md).
7. **JS bundles for the dashboard are IIFE, no build step required** — a plain
   `(function(){...})()` file loadable via `<script>` is a valid bundle.
