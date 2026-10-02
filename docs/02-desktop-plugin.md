# 02 — Desktop plugin SDK (`@hermes/plugin-sdk`)

Verified source: `/Users/crotti/.hermes/hermes-agent/website/docs/developer-guide/desktop-plugin-sdk.md`
(read in full on 2026-09-29). This file is a working digest — when in doubt,
the source doc is authoritative.

## Mental model

A desktop plugin is a **single ESM file** that default-exports a `HermesPlugin`.
It imports exactly one module — `@hermes/plugin-sdk` — plus `react` /
`react/jsx-runtime`. The file is **loaded uncompiled**: no JSX syntax, no build
step, no repo clone. Drop it in `$HERMES_HOME/desktop-plugins/<id>/plugin.js`
(folder name == plugin `id`), the app loads it within seconds and **hot-reloads
every save**.

```javascript
import { host, haptic, useValue } from '@hermes/plugin-sdk'
import { jsx, jsxs } from 'react/jsx-runtime'

export default {
  id: 'hermes-whatsapp-chat',  // must match the folder name
  name: 'Hermes WhatsApp Chat',
  register(ctx) { /* wire contributions */ }
}
```

## The plugin contract

```ts
interface HermesPlugin {
  id: string                  // stable slug; folder name must match
  name?: string               // Settings/about UI
  defaultEnabled?: boolean    // default true; false = opt-in
  register: (ctx: PluginContext) => void
}
```

`register(ctx)` receives a **scoped** context: every contribution id is
namespaced (`<id>:<localId>`) and provenance-stamped (`source: 'plugin:<id>'`),
so two plugins can never collide.

```ts
interface PluginContext {
  source: string                          // 'plugin:hermes-whatsapp-chat'
  register(c: Contribution): () => void   // returns disposer
  registerMany(cs: Contribution[]): () => void
  rest: <T>(path: string, opts?: PluginRestOptions) => Promise<T>   // /api/plugins/<id>/...
  socket: (path: string, onMessage: (data) => void) => () => void  // WS to own namespace; NO-OP on OAuth remotes → always keep a polling fallback
  onEvent: (type: string, listener: (e: GatewayEvent) => void) => () => void  // '*' = all
  onDispose: (fn: () => void) => void
  setTimeout / setInterval / addEventListener   // scoped timers — cleared with the plugin
  os: PluginOs                            // notify, openExternal, revealPath, writeClipboard
  storage: PluginStorage                  // hermes.plugin.<id>.* namespaced persistence
  i18n: { register(bundles) }             // own locale bundles, torn down on reload
}
```

## Contribution areas (the cookbook)

Import area constants from the SDK:

| Surface | `area` | Payload |
|---------|--------|---------|
| Layout pane | `PANES_AREA` (`'panes'`) | `title` + `render` + `data: { placement, dock?, width?, height? }` |
| Full page | `ROUTES_AREA` | `data: { path }` + `render` — mounts in workspace like a built-in view |
| Sidebar nav | `SIDEBAR_NAV_AREA` | `data: { path, label, codicon }` |
| Status bar | `STATUSBAR_AREAS.left/right` | `render` or `data: StatusbarItem` |
| ⌘K palette | `PALETTE_AREA` | `data: { id, label, keywords, run }` |
| Keybind | `KEYBINDS_AREA` | `data: { id, label, category, defaults: ['mod+shift+b'], run }` |
| Theme | `THEMES_AREA` | `data: DesktopTheme` |
| Composer | `COMPOSER_AREAS.*` | slots / middleware / attachment providers |
| Session rows | `SESSION_ROW_AREAS.leading/trailing` | `data: { render: ({sessionId}) => node or null }` |
| Transcript directive | `TRANSCRIPT_DIRECTIVE_AREA` | `data: { name, render }` — model writes `::name{...}` |
| Page header | `WORKSPACE_PAGE_HEADER_AREA` | via `<WorkspacePageHeaderControl>` |

**Panes**: `placement` is `'main' | 'left' | 'right' | 'top' | 'bottom'`
(semantic role, stacks as tabs). To land on a specific edge add a dock gesture:
`data: { placement: 'bottom', dock: { pane: 'workspace', pos: 'bottom' }, height: '200px' }`.

**Full page + sidebar nav** (the shape for the board):

```javascript
import { ROUTES_AREA, SIDEBAR_NAV_AREA } from '@hermes/plugin-sdk'

ctx.registerMany([
  { id: 'page', area: ROUTES_AREA, data: { path: '/wa-board' },
    render: () => jsx(BoardPage, {}) },
  { id: 'nav', area: SIDEBAR_NAV_AREA,
    data: { path: '/wa-board', label: 'Conversazioni', codicon: 'comment-discussion' } }
])
```

Navigate from anywhere with `host.navigate('/wa-board')`.

## Host API (the verbs)

```ts
host.state.activeSessionId / .busy / .busyBySession / .focusedSessionId
       / .focusedStoredSessionId / .cwd / .gateway / .model / .profile / .viewport
host.notify({ kind, message, title? })      // in-app toast
ctx.os.notify({ title, body })              // native OS notification (fires only when app unfocused)
host.navigate('/route')
host.request(method, params)                // gateway JSON-RPC (sessions, config, kanban, ...)
host.onEvent(type, fn)                      // gateway event stream; listeners isolated
host.composer.getDraft/setDraft/insertText/submit/focus(sessionId|null)
host.sessions.pin/reorder/reorderPinned/setColor
host.skills.list/setEnabled ; host.toolsets.list/setEnabled ; host.profiles.list
```

State atoms are readonly: `.get()` in handlers, `useValue(atom)` in components.
`host.state.gateway` is the WS connection, NOT turn-busy — disable actions on
`busyBySession`, never on `gateway`.

## Data layer — React Query + nanostores

Plugins share the app's single `QueryClient`: cache, dedupe, poll, invalidate
like core screens. **Never hand-roll a fetch loop.**

```javascript
import { useQuery, useMutation, useQueryClient, queryClient, atom, useValue } from '@hermes/plugin-sdk'

const { data } = useQuery({
  queryKey: ['wa-board', 'columns'],
  queryFn: () => ctx.rest('/board')
})

// invalidate from OUTSIDE React (e.g. a socket frame or gateway event):
import { queryClient } from '@hermes/plugin-sdk'
host.onEvent('plugin.hermes-whatsapp-chat.conversations.updated', () => {
  queryClient.invalidateQueries({ queryKey: ['wa-board', 'columns'] })
})
```

## Backend access (the plugin's own Python half)

```javascript
const load = () => ctx.rest('/board')                          // GET /api/plugins/<id>/board
const act  = () => ctx.rest('/action', { method: 'POST', body: { go: true } })
const stop = ctx.socket('/events', frame => refresh(frame))    // live twin; no-op on OAuth remotes
```

`ctx.rest` is profile-aware and rejects path traversal. The backend pushes
events via `broadcast_plugin_event()` (see 04-python-backend.md) — subscribe
with `host.onEvent('plugin.<id>.<event>', ...)`.

## UI kit & theming

Import the app's real components: `Button`, `Input`, `Textarea`, `Select*`,
`Switch`, `Checkbox`, `SegmentedControl`, `Tabs*`, `Dialog*`, `ConfirmDialog`,
`DropdownMenu*`, `ContextMenu*`, `Popover*`, `Tip`/`Tooltip*`, `Badge`,
`Kbd`/`KbdGroup`, `SearchField`, `ScrollArea`, `Separator`, `Skeleton`,
`GlyphSpinner`, `Loader`, `EmptyState`, `ErrorState`, `CopyButton`, `StatusDot`,
`LogView`, `Codicon`, `DecodeText`, `SandboxedFrame`. Plus `cn`, `icons.*`,
`haptic`, formatters (`relativeTime`, `fmtDateTime`), `useI18n`.

**Style with theme variables only** — `var(--ui-text-secondary)`,
`var(--ui-text-tertiary)`, `var(--ui-stroke-secondary)`, `var(--ui-accent)`.
Never hardcode colors. Leave the background alone.

## Pitfalls (verified — each one is a real failure mode)

- **JSX won't parse.** Use `jsx()` / `jsxs()` from `react/jsx-runtime`.
- **Only 3 specifiers resolve**: `@hermes/plugin-sdk`, `react`,
  `react/jsx-runtime`. Everything else fails the load, on purpose.
- **Reference only what you imported** — a forgotten import is a
  `ReferenceError` at render.
- **Read state imperatively in handlers** (`$atom.get()`), never from render
  closures — rapid events see stale values otherwise.
- **Bare globals are not tracked**: `window.setInterval` / raw
  `addEventListener` / appended `<style>` survive disable and hot-reload (ES
  modules can't unload; hot-edit loops stack live copies). Use `ctx.setTimeout`
  / `ctx.setInterval` / `ctx.addEventListener` / `ctx.onDispose`.
- **Module evaluation has a 10 s deadline** — no top-level `await`; wait inside
  `register()`.
- **Don't poll faster than a few seconds**; prefer `host.onEvent` / `ctx.socket`
  and let React Query dedupe. Every socket consumer needs a polling fallback.
- **One id, one file** — duplicate ids load first-wins, later shows `duplicate id`.
- **Canvas panes must track their container** with a `ResizeObserver`.

## Reload / troubleshooting

- Save the file → hot reload within seconds. If nothing: ⌘K → **Reload desktop
  plugins**. Error toast names the failure.
- `ctx.rest` 404 → backend not mounted: check `plugins.enabled` in config.yaml,
  restart the gateway, tail `~/.hermes/logs/errors.log`.
- Tail logs: `hermes logs gui -f`.

## Reference implementation

The bundled kanban dashboard plugin at
`/Users/crotti/.hermes/hermes-agent/plugins/kanban/` is a full board UI — but it
is a **dashboard** plugin (03), not desktop. For desktop UX patterns (panes,
pages, palette, i18n, storage) use [examples/desktop-plugin-minimal.js](examples/desktop-plugin-minimal.js)
plus `strike-freedom-cockpit` and `example-dashboard` in
https://github.com/NousResearch/hermes-example-plugins.
