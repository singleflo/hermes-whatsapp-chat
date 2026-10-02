# 03 — Web dashboard plugin (`hermes dashboard`)

Verified sources: `/Users/crotti/.hermes/cache/web/hermes-agent.nousresearch.com-75a3e3b380.md`
(full page: `https://hermes-agent.nousresearch.com/docs/user-guide/features/extending-the-dashboard`)
and the live bundled plugin at `/Users/crotti/.hermes/hermes-agent/plugins/kanban/dashboard/`.

## What it is

A dashboard plugin is a directory with `manifest.json`, a pre-built **IIFE** JS
bundle, optional CSS, and an optional Python backend (`plugin_api.py` with
FastAPI routes). Plugins live inside `~/.hermes/plugins/<name>/dashboard/` — one
package can extend CLI/gateway AND dashboard from a single install. **Drop-in at
runtime**: no repo clone, no `npm run build`, no patching. A plain
`(function(){...})()` file loadable via `<script>` IS a valid bundle.

## Directory layout

```
~/.hermes/plugins/hermes-whatsapp-chat/
├── plugin.yaml                    # optional — agent half manifest
├── __init__.py                    # optional — agent half hooks
└── dashboard/                     # dashboard extension
    ├── manifest.json              # required
    └── dist/
        ├── index.js               # required — IIFE bundle
        └── style.css              # optional
    └── plugin_api.py              # optional — FastAPI routes (mounts at /api/plugins/<name>/)
```

## Manifest reference (verified from the docs)

```json
{
  "name": "hermes-whatsapp-chat",
  "label": "Conversazioni",
  "description": "Kanban board of WhatsApp conversations",
  "icon": "MessageSquare",
  "version": "0.1.0",
  "tab": { "path": "/wa-board", "position": "after:skills" },
  "slots": [],
  "entry": "dist/index.js",
  "css": "dist/style.css",
  "api": "plugin_api.py"
}
```

| Field | Required | Notes |
| --- | --- | --- |
| `name` | Yes | lowercase, hyphens; used in URLs and registration |
| `label` | Yes | nav tab label |
| `icon` | No | Lucide icon name (mapped: `Activity, BarChart3, Clock, Code, Database, Eye, FileText, Globe, Heart, KeyRound, MessageSquare, Package, Puzzle, Settings, Shield, Sparkles, Star, Terminal, Wrench, Zap`); unknown → `Puzzle` |
| `version` | No | semver, default `0.0.0` |
| `tab.path` | Yes | URL path for the tab |
| `tab.position` | No | `"end"` (default), `"after:<path>"`, `"before:<path>"` (path segment, no leading slash) |
| `tab.override` | No | replace a built-in page instead of adding a tab |
| `tab.hidden` | No | register without a nav tab (slot-only plugins) |
| `entry` | Yes | bundle path relative to `dashboard/`, default `dist/index.js` |
| `css` | No | injected as `<link>` |
| `api` | No | Python file with FastAPI routes, mounted at `/api/plugins/<name>/` |

## The Plugin SDK (`window.__HERMES_PLUGIN_SDK__`)

Plugins never import React directly — everything is on the global SDK:

```javascript
(function () {
  const SDK = window.__HERMES_PLUGIN_SDK__
  const { React } = SDK
  const { Card, CardHeader, CardTitle, CardContent, Button, Badge } = SDK.components

  function BoardPage() {
    return React.createElement(Card, null, /* ... */)
  }

  window.__HERMES_PLUGINS__.register("hermes-whatsapp-chat", BoardPage)
})()
```

Available on the SDK (verified):
- `SDK.React` + `SDK.hooks.*` (useState, useEffect, useCallback, useMemo, useRef, useContext, createContext)
- `SDK.components.*` — shadcn/ui primitives: Card, CardHeader, CardTitle, CardContent, Badge, Button, Input, Label, Select, SelectOption, Separator, Tabs, TabsList, TabsTrigger, PluginSlot
- `SDK.api` — typed client: `getStatus()`, `getSessions(n)`, `getConfig()`, ...
- `SDK.fetchJSON(path)` — raw fetch with session auth injected; for
  `/api/plugins/<name>/...` custom routes
- `SDK.utils.cn` (Tailwind class merger), `SDK.utils.timeAgo`, `SDK.utils.isoTimeAgo`
- `SDK.useI18n`

### Calling your plugin's backend

```javascript
SDK.fetchJSON("/api/plugins/hermes-whatsapp-chat/board")
  .then(data => console.log(data))
  .catch(err => console.error("API call failed:", err))
```

### Shell slots (inject into the app chrome without a tab)

```javascript
window.__HERMES_PLUGINS__.registerSlot("hermes-whatsapp-chat", "sidebar", MySidebar)
window.__HERMES_PLUGINS__.registerSlot("hermes-whatsapp-chat", "sessions:top", MyBanner)
```

Shell-wide: `backdrop`, `header-left`, `header-right`, `header-banner`,
`sidebar` (cockpit layout only), `pre-main`, `post-main`, `footer-left`,
`footer-right`, `overlay`.
Page-scoped: `sessions:top/bottom`, `analytics:*`, `logs:*`, `cron:*`,
`skills:*`, `config:*`, `env:*`, `docs:*`, `chat:*`.

Re-registering the same `(plugin, slot)` pair replaces it (HMR-friendly).

## Backend (shared with the desktop half)

`dashboard/plugin_api.py` exporting `router = APIRouter()` mounts at
`/api/plugins/hermes-whatsapp-chat/`. Backend code runs inside the gateway
process and can import the hermes-agent codebase directly (`hermes_cli.*`,
`hermes_state`, ...). Full backend reference in
[04-python-backend.md](04-python-backend.md). **Gated by `plugins.enabled`** in
config.yaml (same security boundary as the desktop half).

The bundled kanban plugin also demonstrates a **WebSocket events endpoint**
(`@router.websocket("/events")`) that tails an append-only table with a short
poll — see `plugin_api.py` lines ~1677-1807 at
`/Users/crotti/.hermes/hermes-agent/plugins/kanban/dashboard/plugin_api.py` and
[06-case-study-kanban.md](06-case-study-kanban.md).

## Discovery & reload

- Restart the web UI, or `GET /api/dashboard/plugins/rescan`, after dropping the
  folder in place.
- Uninstall: `rm -rf ~/.hermes/plugins/<name>` + rescan.

## Pitfalls

- The dashboard SDK and the desktop SDK are **unrelated** — do not port code
  between them blindly; only the backend namespace is shared.
- Use React via `SDK.React` and components via `SDK.components` — a direct
  React import is not available in the bundle.
- Style with theme vars / Tailwind classes consistent with the dashboard; no
  hardcoded colors.
- Keep the bundle a single IIFE file loadable via `<script>` (a bundler is
  optional convenience, not a requirement).
