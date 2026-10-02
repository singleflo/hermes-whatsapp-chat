# 04 — Python backend: plugin.yaml, register(ctx), plugin_api.py

Verified sources: `/Users/crotti/.hermes/hermes-agent/website/docs/user-guide/features/plugins.md`
(read lines 1-300 on 2026-09-29), the bundled kanban plugin's
`plugin_api.py` (1807 lines, read in full), and the desktop-plugin-sdk doc's
backend sections.

## The three Python layers of a unified plugin

1. **`plugin.yaml`** — manifest for the agent half (name, version, description,
   optional `requires_env`).
2. **`__init__.py`** with `register(ctx)` — agent-side capabilities (tools,
   hooks, slash commands). Optional for hermes-whatsapp-chat (only if it later
   adds an agent tool).
3. **`dashboard/plugin_api.py`** — FastAPI backend mounted at
   `/api/plugins/<id>/`, shared by desktop AND dashboard halves.

## plugin.yaml

```yaml
name: hermes-whatsapp-chat
version: "0.1.0"
description: Kanban board of WhatsApp conversations (desktop + dashboard + backend)
```

## register(ctx) — the agent half (minimal)

```python
"""hermes-whatsapp-chat — agent half. Currently minimal; the value is the
backend (plugin_api.py) and the two UI halves."""

def register(ctx):
    # Future: ctx.register_tool("search_conversations", ...) over the archive DB
    pass
```

Full `ctx.*` capability list (verified from the docs) for when it's needed:

| Capability | API |
|---|---|
| Add tools | `ctx.register_tool(name=..., toolset=..., schema=..., handler=...)` |
| Add hooks | `ctx.register_hook("post_tool_call", cb)` |
| Add slash commands | `ctx.register_command(name, handler, description)` |
| Dispatch tools | `ctx.dispatch_tool(name, args)` |
| Add CLI commands | `ctx.register_cli_command(name, help, setup_fn, handler_fn)` |
| Inject messages | `ctx.inject_message(content, role="user", session_key=...)` |
| Bundle skills | `ctx.register_skill(name, path)` → `plugin:skill` |
| Gate on env | `requires_env: [API_KEY]` in plugin.yaml |
| LLM one-shot | `ctx.llm.complete(...)` / `ctx.llm.complete_structured(...)` |
| Call MCP | `ctx.call_mcp(server, tool, arguments, timeout=30)` |
| Register platform | `ctx.register_platform(name, label, adapter_factory, check_fn, ...)` |
| Approval transport | `ctx.register_approval_transport(name, present_fn)` |

**Opt-in gate**: general plugins are disabled by default. `plugin_api.py` is
imported only when the plugin is in `plugins.enabled` in `config.yaml`:

```bash
hermes plugins enable hermes-whatsapp-chat
```

Timeouts (config.yaml `plugins.*`, verified defaults): `load_timeout_seconds: 10`
(import + register), `hook_callback_timeout: 30` (hot-path hooks),
`clone_timeout_seconds: 300`.

## plugin_api.py — the backend (modeled on the bundled kanban plugin)

Full skeleton in [plugin/dashboard/plugin_api.py](../plugin/dashboard/plugin_api.py). Key
verified patterns from the kanban plugin worth reusing:

### Structure

```python
from fastapi import APIRouter, HTTPException, Query, WebSocket
from pydantic import BaseModel

router = APIRouter()
```

- **Thin handlers around a domain module.** Kanban's `plugin_api.py` wraps
  `hermes_cli.kanban_db` — "the same code paths the CLI and gateway use, so the
  surfaces cannot drift". For hermes-whatsapp-chat, the domain module is the
  conversations DB layer (owned by the pipeline, not the plugin). Import it,
  don't duplicate it.
- **Pydantic bodies**: `class CreateTaskBody(BaseModel)` with explicit fields —
  set every field explicitly, never rely on provider defaults.
- **Error mapping helpers**: map domain `ValueError` → 400, missing → 404,
  conflict → 409, unexpected → 500 with a prefix. Kanban's `_map_errors`,
  `_require`, `_conflict` context managers are a proven shape.
- **N+1 avoidance**: board endpoints use aggregate queries / window functions
  (`link_counts`, `comment_counts`, `latest_summaries` in one query each). The
  conversations board must do the same (one query per column, not per card).
- **Card payloads carry derived fields**: `age`, previews truncated to ~200
  chars on list endpoints; full text only on the detail endpoint.
- **`coalesced_read`**: kanban wraps `GET /board` with
  `hermes_cli.web_read_coalescing.coalesced_read` to collapse concurrent reads.

### Live events — two push channels (verified)

**1. `broadcast_plugin_event` (recommended for desktop):**

```python
from hermes_cli.plugin_events import broadcast_plugin_event

broadcast_plugin_event("hermes-whatsapp-chat", "conversations.updated",
                       {"chat_jid": "39333...@s.whatsapp.net", "state": "nuova"})
# wire name: plugin.hermes-whatsapp-chat.conversations.updated
```

Rules (verified): `plugin_id` = catalog name `[a-z0-9_-]{1,64}` (no dots);
`event` is the BARE dotted name (`"conversations.updated"`); payload is a JSON
dict. Fire-and-forget; a wedged client is skipped. Reaches Desktop windows when
called from `hermes serve`; is a logged no-op in gateway/cron processes with no
attached client. The desktop half subscribes with
`host.onEvent('plugin.hermes-whatsapp-chat.conversations.updated', ...)`.

**2. WebSocket endpoint (kanban's `/events` pattern):**

```python
@router.websocket("/events")
async def stream_events(ws: WebSocket):
    await ws.accept()
    # tail an append-only table since a cursor, short poll (~0.3s) inside
    # asyncio.wait_for(ws.receive(), timeout=POLL) so a disconnect is detected
    # even with no events; per-socket dedicated connection + single-thread
    # executor (SQLite connections are thread-affine)
```

Kanban's exact shape: `_EventTail` class — one SQLite connection per socket
used only on a dedicated `ThreadPoolExecutor(max_workers=1)`, cursor semantics
("None starts at the current tail; only an explicit `since` replays history"),
WAL-friendly. Copy this shape if the dashboard half needs live updates.

### WebSocket auth (kanban's `_ws_upgrade_authorized`)

Reuse the dashboard's canonical gate so the endpoint can never drift from core
auth:

```python
def _ws_upgrade_authorized(ws: WebSocket) -> bool:
    try:
        from hermes_cli import web_server_chat as _ws
    except Exception:
        return True   # bare-FastAPI test harness
    return bool(_ws._ws_auth_ok(ws))
```

## What the backend serves (hermes-whatsapp-chat contract)

| Route | Purpose |
|---|---|
| `GET /board` | columns + cards (chat_jid, contact, last_message preview, state, priority, age, unread) |
| `GET /chats/{chat_jid}` | detail: full recent thread, contact metadata, state history |
| `POST /chats/{chat_jid}/state` | transition state (muta / riapri / chiudi / escalate) — guarded, logged |
| `GET /stats` | per-state counts, oldest unanswered age (for the HUD / statusbar) |
| `GET /health` | DB reachable, row counts (desktop half shows a clear error state when down) |

All reads go to the conversations archive DB (schema owned by the pipeline —
see 08-spec). The backend NEVER writes messages; only conversation-state
transitions (its own table or kanban cards) and reads.

## Security notes (verified)

- Backend runs **inside the gateway process** with full hermes-agent import
  access — treat it as trusted code (it's yours).
- Mount is namespaced by construction; `ctx.rest` rejects path traversal.
- Enable-gate (`plugins.enabled`) is the security boundary for third-party code
  (GHSA-mcfc-hp25-cjv7).
- Project-local plugins (`./.hermes/plugins/`) require
  `HERMES_ENABLE_PROJECT_PLUGINS=true` — never rely on them for prod.
