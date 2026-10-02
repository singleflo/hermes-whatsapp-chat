---
name: hermes-plugin-development
description: "Use when building Hermes plugins: surfaces, TDD, dev loop."
version: 1.0.0
author: Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [hermes, plugins, desktop, dashboard, fastapi, tdd]
    category: software-development
    related_skills: [hermes-desktop-plugins, hermes-entity-automation]
---

# Hermes Plugin Development

Procedure for building Hermes plugins on this machine, verified against the
local source tree. For desktop-UI specifics (panes, contribution areas, SDK
surface) load the bundled `hermes-desktop-plugins` skill — this one covers
what it does not: the unified-package architecture, the Python backend, TDD,
install/enable, and the dev loop.

## When to Use

- Building or extending a Hermes plugin of any kind (desktop UI, web dashboard
  tab, Python backend / agent half, or a unified package shipping several).
- Setting up the TDD loop for a plugin's `plugin_api.py` backend.
- Installing/enabling a plugin locally and debugging why a half doesn't load.

## Ground truth — read before building

- Authoritative, on disk: `/Users/crotti/.hermes/hermes-agent/website/docs/` —
  `developer-guide/desktop-plugin-sdk.md` (desktop),
  `user-guide/features/extending-the-dashboard.md` (web dashboard),
  `user-guide/features/plugins.md` (Python agent half).
- Reference implementations: bundled plugins under
  `/Users/crotti/.hermes/hermes-agent/plugins/` — `kanban/` is a complete
  board UI + FastAPI backend + WebSocket events, the best board-style
  reference.
- Online index (regenerated every build, never stale):
  `https://hermes-agent.nousresearch.com/docs/llms.txt`.
- Never claim "Hermes can't do X" from memory — check llms.txt or the local
  tree first.
- This repo is the plugin: `plugin/` (Hermes package), `sidecar/` (independent
  WhatsApp channel), `tests/test_plugin_api.py`, `docs/` (kit docs), `AGENTS.md`.

## Procedure

1. **Design first; act only on an explicit go.** While the user reasons aloud,
   answer with analysis and trade-offs only — no tool actions, no scaffolded
   folders. Build after an explicit "vai". Confirm the folder/repo name before
   scaffolding: it becomes the GitHub repo, and renaming after content has
   landed wastes a full pass.
2. **Pick the surfaces.** Desktop UI / dashboard UI / Python backend / agent
   half. More than one → unified package (layout in kit `01-architecture.md`):
   `plugin.yaml`, optional `__init__.py` (`register(ctx)`),
   `dashboard/{manifest.json, plugin_api.py, dist/}`, `desktop/plugin.js`.
3. **Backend first, TDD.** Pattern from Hermes' own kanban plugin tests (kit
   `05-testing-tdd.md`): load the REAL `plugin_api.py` via
   `importlib.util.spec_from_file_location`, mount its router in a bare
   FastAPI app under `/api/plugins/<id>`, drive it with
   `fastapi.testclient.TestClient`, point the DB at a throwaway via env var
   (`tmp_path` + `monkeypatch.setenv`). One red→green slice at a time;
   monkeypatch only external boundaries (LLM clients), never internals.
4. **Own dev venv.** `uv sync --python 3.11` (deps in root `pyproject.toml`).
5. **Install and enable.** `ln -s /Users/crotti/VSC/TOOLS/hermes-whatsapp-chat/plugin ~/.hermes/plugins/hermes-whatsapp-chat`; `hermes
   plugins enable <id>` (this gates the Python backend); restart the gateway
   (backend routes mount at startup). The desktop half is auto-copied by the
   Electron main process into `~/.hermes/desktop-plugins/<id>/` — toggle it on
   in Capabilities → Plugins. Dashboard: restart the web UI or
   `GET /api/dashboard/plugins/rescan`.
6. **Dev loop.** Desktop `plugin.js` hot-reloads on save (⌘K → *Reload desktop
   plugins* if stale); backend changes need a gateway restart. Debug with
   `hermes logs gui -f`; backend errors land in `~/.hermes/logs/errors.log`.

## Pitfalls

- The three surfaces (desktop / dashboard / Python) are unrelated SDKs; ONLY
  the `plugin_api.py` namespace (`/api/plugins/<id>/`) is shared between the
  two UIs — never port UI code between them blindly.
- Desktop `plugin.js` loads uncompiled: `jsx()`/`jsxs()` calls only, and only
  three importable specifiers (`@hermes/plugin-sdk`, `react`,
  `react/jsx-runtime`) — anything else fails the load.
- `plugins.enabled` in config.yaml gates the Python backend; toggling the
  desktop Plugins panel does NOT import Python. `ctx.rest` 404 ⇒ backend not
  mounted (not enabled, or gateway not restarted).
- `ctx.socket` is a no-op on OAuth remotes — every consumer ships a polling
  fallback. Never poll faster than a few seconds; prefer
  `broadcast_plugin_event(<id>, "event.name", payload)` +
  `host.onEvent('plugin.<id>.<event.name>')` and let React Query dedupe.
- `hermes plugins update` / Rescan refreshes the desktop copy from the package
  source — hand-edits to `~/.hermes/desktop-plugins/<id>/` are overwritten;
  edit the package source.
- Against a remote gateway only the backend half runs remotely — the desktop
  half must degrade to a clear error state, never crash, when
  `/api/plugins/<id>/` is unreachable.
- Never hand-edit `~/.hermes/config.yaml` — use `hermes config set KEY VAL`
  (a stray indent breaks the live gateway). Treat
  `/Users/crotti/.hermes/hermes-agent/` as read-only reference.
- Board/card endpoints: truncate previews (~200 chars) on list routes, derive
  ages server-side, aggregate queries only — never a per-card query (N+1).
- Verify every template compiles before delivering (`py_compile`, `node
  --check`, `json.load`) — a kit whose skeletons fail syntax checks is worse
  than no kit.
