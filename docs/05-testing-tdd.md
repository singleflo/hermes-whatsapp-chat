# 05 — Testing & TDD

Verified sources: `/Users/crotti/.hermes/hermes-agent/tests/plugins/test_kanban_estimate.py`
(read in full), `test_kanban_read_admission.py`, and the TDD skill at
`/Users/crotti/.agents/skills/tdd/` (copied to [skills/tdd.md](skills/tdd.md)).

## The TDD loop (from the skill — the rules that matter here)

- **Red before green**: failing test first, then only enough code to pass.
- **Vertical slices**: one test → one implementation → repeat. Never bulk tests.
- **Test only at pre-agreed seams**: the seam for this plugin is the HTTP API
  surface of `plugin_api.py` (the contract both UIs consume) and the DB queries.
- **No tautologies**: expected values come from a known-good literal or the
  spec, never recomputed the way the code does.
- **No implementation coupling**: no mocking of internal collaborators, no
  testing private methods. The kanban tests mock `call_llm` (an external
  boundary), never internals.

## The proven pattern (from Hermes' own kanban plugin tests)

`test_kanban_estimate.py` tests the real `plugin_api.py` — not an import of a
module, but the actual file on disk, mounted in a bare FastAPI app:

```python
import importlib.util, sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.testclient import TestClient

def _load_plugin_router():
    repo_root = Path(__file__).resolve().parents[2]
    plugin_file = repo_root / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_plugin_test", plugin_file)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router

@pytest.fixture
def client(tmp_path, monkeypatch):
    # isolated HERMES_HOME so the DB is a throwaway
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/hermes-whatsapp-chat")
    return TestClient(app)
```

Why this shape is right (verified reasoning):
1. It exercises the **real plugin file** exactly as the gateway loads it.
2. A **bare FastAPI app** isolates the router from the whole dashboard stack —
   no gateway, no auth, no UI.
3. `TestClient` gives real HTTP semantics (status codes, JSON bodies).
4. `tmp_path` + `HERMES_HOME` monkeypatch makes every test self-contained.
5. External boundaries (LLM, DB writes) are monkeypatched at their module —
   `agent.auxiliary_client.call_llm` — never internals.

## Test plan for hermes-whatsapp-chat (the seams)

**Seam 1 — backend HTTP API** (`plugin_api.py` via TestClient):
- `GET /board` returns columns with the seeded conversations in the right
  column (state mapping); empty DB → empty columns, 200.
- `GET /board` card payload: preview truncated, `age` present, unread count.
- `GET /chats/{jid}` 200 with thread; unknown jid → 404.
- `POST /chats/{jid}/state` valid transition → 200 + state changed (read back
  via GET); invalid transition → 409 with the reason; unknown jid → 404.
- `POST /chats/{jid}/state` to `mutata` sets `muted_until` when provided.
- `GET /stats` counts per state; `GET /health` reports DB reachable + counts.
- DB with 400 conversations: `/board` still returns only non-archived cards in
  active columns (the volume requirement).

**Seam 2 — DB query layer** (the domain module, direct SQLite on tmp files):
- State mapping conversation-state → column is exhaustive (unknown state → a
  defined fallback column, never dropped).
- N+1 check: `/board` issues bounded queries (assert via query counting or
  `sqlite3` trace callback — implementation choice, but the invariant is: no
  per-card query).

**Seam 3 — desktop plugin behaviors** (pure functions extracted from
`plugin.js`, testable in Node):
- State → column ordering, badge/urgency derivation, relative-time formatting.
- Keep these as pure exported functions so they're testable without the app.

**What NOT to test**: UI rendering pixel-by-pixel, the Hermes app itself, the
WhatsApp bridge (owned by the pipeline, tested there).

## Environment (verified on this machine)

- Python: `/Users/crotti/.hermes/hermes-agent/venv/bin/python` — **3.11.15**,
  has `fastapi` 0.133.1. **`pytest` is NOT installed there** — install it in the
  plugin's own dev venv (see 07-environment-and-paths.md) or `pip install
  pytest` into a dedicated venv; do NOT pip-install into the Hermes venv used by
  the live gateway (use `--user` or a separate venv to avoid polluting the
  production install).
- The kanban tests also import `hermes_cli.kanban_db` — for
  hermes-whatsapp-chat the DB layer is our own module, so tests don't need the
  hermes_cli import chain (simpler). Only `fastapi`, `pydantic`, `pytest`,
  `httpx` (TestClient dep) are needed.

## Running

```bash
cd /Users/crotti/VSC/TOOLS/hermes-whatsapp-chat
# from repo root once the plugin source lives here (see 07 for layout)
<dev-venv-python> -m pytest tests/ -v
```

The suite is [tests/test_plugin_api.py](../tests/test_plugin_api.py) — first
test (`GET /health`) is the tracer bullet: RED (no route) → implement → GREEN.
