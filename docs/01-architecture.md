# 01 — Architecture: the three plugin surfaces

Verified against: `/Users/crotti/.hermes/hermes-agent/website/docs/developer-guide/desktop-plugin-sdk.md`,
`website/docs/user-guide/features/extending-the-dashboard.md`, `website/docs/user-guide/features/plugins.md`,
and the live tree at `/Users/crotti/.hermes/hermes-agent/plugins/`.

## The three surfaces (do not confuse them)

| Surface | App | Entry point | Lives in | SDK |
|---|---|---|---|---|
| **Desktop** | `hermes desktop` (Electron) | `plugin.js` (ESM, default-export `HermesPlugin`) | `$HERMES_HOME/desktop-plugins/<id>/plugin.js` | `import ... from '@hermes/plugin-sdk'` |
| **Web dashboard** | `hermes dashboard` (browser) | `manifest.json` + pre-built IIFE bundle | `~/.hermes/plugins/<id>/dashboard/` | global `window.__HERMES_PLUGIN_SDK__` |
| **Python (CLI/gateway)** | CLI, gateway, agent turns | `plugin.yaml` + `__init__.py` with `register(ctx)` | `~/.hermes/plugins/<id>/` | `ctx.register_tool()`, `ctx.register_hook()`, ... |

They do not share code or APIs. **The ONLY shared thing** is the backend
namespace: a `dashboard/plugin_api.py` exporting `router = APIRouter()` mounts at
`/api/plugins/<id>/` and is reachable from BOTH the desktop half (`ctx.rest`)
and the dashboard half (`SDK.fetchJSON`).

## The unified package (the shape of hermes-whatsapp-chat)

One installable folder carrying all three halves:

```
~/.hermes/plugins/hermes-whatsapp-chat/      # ONE installable folder
├── plugin.yaml                              # agent half: name/version/description
├── __init__.py                              # optional: register(ctx) — only if the plugin adds agent tools
├── dashboard/
│   ├── manifest.json                        # { "name": "hermes-whatsapp-chat", "api": "plugin_api.py", "tab": {...} }
│   ├── plugin_api.py                        # backend FastAPI routes → /api/plugins/hermes-whatsapp-chat/
│   └── dist/
│       ├── index.js                         # dashboard UI (IIFE)
│       └── style.css                        # optional
└── desktop/
    └── plugin.js                            # desktop UI half (ESM)
```

**How the desktop half gets loaded from a unified package:** when the package
lands in any local `plugins/` root, the Electron main process copies
`desktop/plugin.js` into `$HERMES_HOME/desktop-plugins/<id>/` beside a
`.hermes-package.json` marker, and the renderer loads it through the same
pipeline as a standalone disk plugin (hot reload included). The copy is refreshed
when the source changes (`hermes plugins update` or **Rescan**), and removed when
the package folder disappears.

**Two enable switches, both default OFF:**
- The desktop half inventories in **Capabilities → Plugins** but stays disabled
  until the user toggles it.
- The Python half is imported only when `hermes-whatsapp-chat` is in
  `plugins.enabled` in `config.yaml` (security boundary, GHSA-mcfc-hp25-cjv7).
- The desktop half degrades gracefully when the backend is off — `ctx.rest`
  returns errors, not crashes.

**Remote backend caveat:** against a remote gateway (VPS hermesR), the remote
box's `~/.hermes/plugins` is NOT reachable as a filesystem — only locally
installed packages contribute a desktop half this way. For a remote backend the
desktop half must be installed separately into local `desktop-plugins/`
(Install from Git with the Desktop target checked). Design consequence: **the
plugin must work with the backend unreachable** (read-only board from cached /
alternative data, clear error states).

## Delivery modes for the desktop half

| Mode | Where | Build step |
|------|-------|------------|
| **Disk** (standalone) | `$HERMES_HOME/desktop-plugins/<id>/plugin.js` | none — plain ESM, loaded uncompiled |
| **Unified package** | `plugins/<id>/desktop/plugin.js` (copied as above) | none |
| **Bundled** (in-tree) | `apps/desktop/src/plugins/<id>/plugin.tsx` | app's Vite build (real JSX allowed) |

For hermes-whatsapp-chat: **unified package**. The backend and both UIs ship,
install, and uninstall as one folder. It can also be installed from git:
`hermes plugins install crottolo/hermes-whatsapp-chat` (or via the catalog /
`hermes://plugin/install?repo=crottolo/hermes-whatsapp-chat` deep link).

## Where `$HERMES_HOME` is

- Default: `~/.hermes`
- Named profile: `~/.hermes/profiles/<name>/` (when running `hermes -p <name>`)
- Resolve in code with `get_hermes_home()`, never hardcode.
- Desktop plugins are **app-level**: one root for every profile/gateway the
  window connects to (unlike agent plugins, which are per-profile).

## Reference implementations on disk (read them)

- `/Users/crotti/.hermes/hermes-agent/plugins/kanban/` — bundled kanban plugin:
  the closest existing thing to this board (board UI + backend + WS events). See
  [06-case-study-kanban.md](06-case-study-kanban.md).
- `/Users/crotti/.hermes/hermes-agent/website/docs/developer-guide/desktop-plugin-sdk.md` —
  the full desktop SDK reference (the source doc for 02-desktop-plugin.md).
- `https://github.com/NousResearch/hermes-example-plugins` — official examples:
  `example-dashboard` (minimal dashboard plugin), `strike-freedom-cockpit`
  (theme+plugin reskin), `plugin-llm-example` (ctx.llm access).

## Decision: what lives where in hermes-whatsapp-chat

| Concern | Half |
|---|---|
| Board columns, cards, drag-drop, filters, detail drawer | Desktop `plugin.js` + Dashboard `dist/index.js` (same UX, two SDKs) |
| Reading conversations DB (SQLite), computing columns/stats | `dashboard/plugin_api.py` (shared backend) |
| Push live updates (new message arrives) | `broadcast_plugin_event("hermes-whatsapp-chat", "conversations.updated", {...})` + `host.onEvent` (desktop) / WS or polling (dashboard) |
| Mute/unmute a conversation, change state | `plugin_api.py` POST routes → DB write |
| Optional: agent tool "search conversations" | `__init__.py` `ctx.register_tool` (only if needed later) |

## Data-flow context (where conversation data comes from)

This plugin is a VIEW over data produced by the WhatsApp pipeline already
designed (see [08-spec-wa-board.md](08-spec-wa-board.md) for the full picture):

```
WhatsApp bridge (Hermes native Baileys, bot mode, dedicated number)
   → inbound message → session agent (full context, per-chat session)
   → hook: archive every message in/out to SQLite (deterministic, zero LLM)
   → classifier (Jev pattern) tags conversation state
   → THIS PLUGIN renders the board from that DB + pushes live updates
```

The plugin does NOT own the data pipeline. It reads the DB and offers
state-change actions. Keep that separation: the DB schema is the contract.
