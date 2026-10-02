# 07 — Environment, paths, and the dev loop

All paths verified on this machine (macOS, 2026-09-29).

## Absolute paths — where to look when developing

### Hermes installation (the runtime we extend)

| What | Path |
|---|---|
| Hermes launcher | `/Users/crotti/.local/bin/hermes` (execs `/Users/crotti/.hermes/hermes-agent/venv/bin/hermes`) |
| Source tree | `/Users/crotti/.hermes/hermes-agent/` |
| Hermes venv Python | `/Users/crotti/.hermes/hermes-agent/venv/bin/python` (3.11.15, has fastapi 0.133.1; NO pytest) |
| User plugins dir | `/Users/crotti/.hermes/plugins/` |
| Desktop plugins dir | `/Users/crotti/.hermes/desktop-plugins/` |
| Main config | `/Users/crotti/.hermes/config.yaml` |
| Secrets (.env) | `/Users/crotti/.hermes/.env` |
| Logs | `/Users/crotti/.hermes/logs/` (`errors.log`, `gateway.log`; GUI: `hermes logs gui -f`) |
| Kanban reference plugin | `/Users/crotti/.hermes/hermes-agent/plugins/kanban/` |
| Kanban plugin tests | `/Users/crotti/.hermes/hermes-agent/tests/plugins/test_kanban_estimate.py` |
| Desktop SDK doc | `/Users/crotti/.hermes/hermes-agent/website/docs/developer-guide/desktop-plugin-sdk.md` |
| Dashboard plugin docs | `/Users/crotti/.hermes/hermes-agent/website/docs/user-guide/features/extending-the-dashboard.md` and `plugins.md` |
| Docs index (always current) | `https://hermes-agent.nousresearch.com/docs/llms.txt` |

### Skills (the agent skills used by this kit)

| Skill | Live path |
|---|---|
| Desktop plugins | `/Users/crotti/.hermes/skills/hermes-desktop-plugins/` (SKILL.md + templates/plugin.js) |
| Entity automation (webhook+kanban+profiles) | `/Users/crotti/.hermes/skills/business/hermes-entity-automation/` |
| Hermes agent hub (webhooks ref, background systems) | `/Users/crotti/.hermes/skills/autonomous-ai-agents/hermes-agent/` (references/webhooks.md, references/background-systems.md) |
| TDD | `/Users/crotti/.agents/skills/tdd/` (SKILL.md, tests.md, mocking.md) |

Copies of all of these live in this kit's [skills/](skills/) folder — but the
live paths are authoritative (they update with Hermes).

### This repo

| What | Path |
|---|---|
| Repo root | `/Users/crotti/VSC/TOOLS/hermes-whatsapp-chat/` |
| Plugin source (unified package) | `plugin/` |
| Tests | `tests/` |
| Docs, skill copies, examples | `docs/`, `docs/skills/`, `docs/examples/` |

## Dev environment setup (verified commands)

```bash
# 1. Dev venv for the plugin (do NOT pollute the Hermes runtime venv)
cd /Users/crotti/VSC/TOOLS/hermes-whatsapp-chat
python3.11 -m venv .venv              # /Users/crotti/.local/bin/python3.11 exists (verified)
.venv/bin/pip install fastapi pydantic pytest httpx uvicorn
# (hermes' venv python is 3.11.15; match it: python3.11)

# 2. Run tests (TDD loop)
.venv/bin/python -m pytest tests/ -v

# 3. Validate the plugin structure like Hermes does
hermes plugins validate .              # if run from the plugin folder
```

`httpx` is required — `fastapi.testclient.TestClient` is built on it.

## Repo layout

```
hermes-whatsapp-chat/                     # repo root = /Users/crotti/VSC/TOOLS/hermes-whatsapp-chat
├── README.md                             # repo README (EN — public repo convention)
├── AGENTS.md                             # guidelines for AI assistants
├── docs/
│   ├── 01..08-*.md                       # this documentation
│   ├── skills/                           # skill copies (above)
│   └── examples/desktop-plugin-minimal.js  # official desktop skill template
├── plugin/                               # THE PLUGIN (unified package — copy to ~/.hermes/plugins/ to install)
│   ├── plugin.yaml
│   ├── __init__.py                       # optional — only if register(ctx) is needed
│   ├── dashboard/
│   │   ├── manifest.json
│   │   ├── plugin_api.py
│   │   └── dist/ (index.js, style.css)
│   └── desktop/
│       └── plugin.js
└── tests/
    └── test_plugin_api.py                # TestClient pattern (05-testing-tdd.md)
```

## The dev loop (verified workflow)

1. **Backend first (TDD)**: write failing test → implement route in
   `plugin/dashboard/plugin_api.py` → green. Run with `.venv/bin/python -m pytest tests/ -v`.
2. **Install for real**: `cp -r plugin ~/.hermes/plugins/hermes-whatsapp-chat/`
3. **Enable**: `hermes plugins enable hermes-whatsapp-chat` (backend gate) —
   then restart the gateway: `hermes gateway restart` (or the desktop Restart
   Gateway action; backend routes mount at startup).
4. **Dashboard half**: open `hermes dashboard` → tab appears after restart or
   `GET /api/dashboard/plugins/rescan`.
5. **Desktop half**: with the unified package in place, the Electron main
   process copies `desktop/plugin.js` into `~/.hermes/desktop-plugins/`; toggle
   it on in **Capabilities → Plugins**. Hot-reloads on every save; ⌘K →
   *Reload desktop plugins* if it doesn't appear.
6. **Iterate UI**: edit `desktop/plugin.js` in the installed folder (or rescan
   after editing the package source); errors surface as a toast naming the failure.
7. **Debug**: `hermes logs gui -f`; backend errors in `~/.hermes/logs/errors.log`
   (`Failed to load plugin <id> API routes`).

## Stop conditions / gotchas

- `ctx.rest` 404 in the desktop half ⇒ backend not mounted (not enabled, or
  gateway not restarted) — check `plugins.enabled` first.
- `ctx.socket` never fires on an OAuth remote — by design; keep the polling fallback.
- Remote gateway (VPS hermesR): only the backend half runs there; the desktop
  half connects over the gateway, so features must degrade when
  `/api/plugins/<id>/` is unreachable.
- Desktop plugin fails to load ⇒ almost always an import outside the 3 allowed
  specifiers, or JSX syntax in the file.
- `hermes plugins update` refreshes the desktop copy from the package source —
  if you hand-edited the copy in `desktop-plugins/`, your edits will be
  overwritten; edit the package source.
- Never edit `~/.hermes/config.yaml` by hand — use `hermes config set KEY VAL`
  (a stray indent can break the live gateway).
