"""Jev rules document -> plan -> exit conditions + generated automation rules.

Loads the REAL plugin/dashboard/plugin_api.py (which loads wa_core) and the REAL plugin/scripts/wa.py against a
throwaway SQLite DB. Faked boundaries only: the Hermes call (``jev_rules._run_hermes`` / ``subprocess.run`` inside it)
and the Jev HTTP call (``jev._post``). Real Hermes and real Jev are never called.
"""

from __future__ import annotations

import importlib.util
import io
import json
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILE = REPO_ROOT / "plugin" / "dashboard" / "plugin_api.py"
CLI_FILE = REPO_ROOT / "plugin" / "scripts" / "wa.py"
PREFIX = "/api/plugins/hermes-whatsapp-chat"
CONTACT_JID = "393330001111@s.whatsapp.net"
NOW = int(datetime(2026, 1, 14, 12, 0, tzinfo=ZoneInfo("Europe/Rome")).timestamp())
KEY = "k-test-123"
DOCUMENT = "# Rules\n- If the client is upset, a person answers.\n- Routine questions: the agent drafts a reply.\n"

PLAN: dict[str, Any] = {
    "conditions": [
        {
            "id": "person",
            "label": "Needs a person",
            "description": "The client is upset or asks to talk to a person",
            "min_score": 0.6,
            "actions": [{"type": "takeover"}, {"type": "escalate"}],
            "examples": ["voglio parlare con una persona", "sono molto deluso"],
        },
        {
            "id": "agent",
            "label": "Agent can answer",
            "description": "The client asks a routine question",
            "min_score": 0.7,
            "actions": [{"type": "agent_draft", "instructions": "Be brief"}],
            "examples": ["a che punto è la pratica?"],
        },
    ],
    "else_actions": [{"type": "add_tags", "tags": ["review", "misc"]}],
    "else_examples": ["ciao"],
    "notes": ["Nothing said about opening hours"],
}
# Jev's scores for the example texts (anything else scores 0.1 everywhere)
SCORES = {
    "voglio parlare con una persona": {"person": 0.95},
    "sono molto deluso": {"person": 0.2},  # a miss: lands on Else
    "a che punto è la pratica?": {"agent": 0.9},
    "ciao": {},
}


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# --- Fixtures ----------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("WA_ARCHIVE_DB", str(tmp_path / "wa_board.db"))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-home"))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)
    plugin = _load("hermes_dashboard_plugin_hermes_whatsapp_chat_jev_rules", PLUGIN_FILE)
    core = plugin.core
    app = FastAPI()
    app.include_router(plugin.router, prefix=PREFIX)

    posts: list[dict] = []
    post_results: list = []
    prompts: list[str] = []
    hermes_outputs: list = []

    def fake_post(body, key, timeout):
        data = json.loads(body)
        posts.append({"body": data, "key": key})
        if post_results:
            result = post_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        scores = SCORES.get(data["state"]["latest_message"], {})
        answers = {q: {"type": "noul", "noul": scores.get(q, 0.1)} for q in data["questions"]}
        return 200, json.dumps({"answers": answers, "model": "jev-test", "usage": {"input_tokens": 9}}), {}

    def fake_hermes(prompt, timeout):
        prompts.append(prompt)
        out = hermes_outputs.pop(0) if hermes_outputs else json.dumps(PLAN)
        if isinstance(out, Exception):
            raise out
        return out, {"model": "glm-test"}

    real_run_hermes = core.jev_rules._run_hermes
    monkeypatch.setattr(core.jev, "_post", fake_post)
    monkeypatch.setattr(core.jev_rules, "_run_hermes", fake_hermes)

    conn = core.db.connect()
    yield SimpleNamespace(
        core=core,
        client=TestClient(app),
        conn=conn,
        posts=posts,
        post_results=post_results,
        prompts=prompts,
        hermes_outputs=hermes_outputs,
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        real_run_hermes=real_run_hermes,
    )
    conn.close()


def set_key(env, key: str = KEY) -> None:
    env.core.jev.set_api_key(env.conn, key, NOW)


def mk_rule(env, action: dict, **fields) -> dict:
    res = env.client.post(f"{PREFIX}/automations", json={"name": "rule", "action": action, **fields})
    assert res.status_code == 200, res.text
    return res.json()


def apply_plan(env, plan: Any = None, document: str = DOCUMENT):
    return env.client.post(f"{PREFIX}/jev/apply", json={"document": document, "plan": PLAN if plan is None else plan})


def add_conv(env, text: str = "a che punto è la pratica?") -> int:
    env.conn.execute(
        "INSERT INTO accounts (id, kind, label, port, session_dir, desired, created_at)"
        " VALUES (1,'whatsapp','Main',3017,'s','running',?)",
        (NOW,),
    )
    cur = env.conn.execute(
        "INSERT INTO conversations (account_id, chat_jid, contact_name, phone, state, agent_active, tags, created_at, updated_at)"
        " VALUES (1,?,'Anna','393330001111','new',1,'[]',?,?)",
        (CONTACT_JID, NOW, NOW),
    )
    conv_id = int(cur.lastrowid or 0)
    env.conn.execute(
        "INSERT INTO messages (conversation_id, account_id, direction, author, body, ts, status, source)"
        " VALUES (?,1,'in','contact',?,?,'received','live')",
        (conv_id, text, NOW),
    )
    return conv_id


# --- normalize_plan --------------------------------------------------------------------------


def test_normalize_accepts_a_bare_plan_and_a_generate_result(env):
    jr = env.core.jev_rules
    bare = jr.normalize_plan(PLAN)
    assert [c.id for c in bare.conditions] == ["person", "agent"]
    assert jr.normalize_plan({"plan": PLAN, "model": "x", "checks": None}) == bare
    # a model_dump (None fields for every unused action key) is accepted again
    assert jr.normalize_plan(bare.model_dump()) == bare


def test_normalize_repairs_ids_scores_and_lengths(env):
    jr = env.core.jev_rules
    plan = jr.normalize_plan(
        {
            "conditions": [
                {"label": "  Più urgente! ", "description": " x ", "actions": []},  # missing id -> slug of the label
                {"id": "Bad Id", "label": "123 go", "description": "d"},  # invalid id -> slug, digit prefix
                {"id": "else", "label": "Other", "description": "d"},  # reserved
                {"id": "else", "label": "Other 2", "description": "d"},  # reserved again
                {"label": "Più urgente!", "description": "d"},  # same slug as the first -> _2
                {"id": "ok", "label": "L" * 100, "description": "D" * 600, "min_score": 7},
                {"id": "ok", "label": "Dup", "description": "d", "min_score": -2},
                {"id": "low", "label": "Low", "description": "d", "min_score": "high"},
                {"id": "bool", "label": "Bool", "description": "d", "min_score": True},
            ],
            "else_examples": ["  hello  ", ""],
        }
    )
    ids = [c.id for c in plan.conditions]
    assert ids == ["piu_urgente", "c_123_go", "else_2", "else_3", "piu_urgente_2", "ok", "ok_2", "low", "bool"]
    by_id = {c.id: c for c in plan.conditions}
    assert by_id["piu_urgente"].label == "Più urgente!" and by_id["piu_urgente"].description == "x"
    assert len(by_id["ok"].label) == 80 and len(by_id["ok"].description) == 500
    assert by_id["ok"].min_score == 1.0 and by_id["ok_2"].min_score == 0.0
    assert by_id["low"].min_score == 0.7 and by_id["bool"].min_score == 0.7
    assert plan.else_examples == ["hello"]


def test_normalize_drops_action_keys_the_type_does_not_use(env):
    jr = env.core.jev_rules
    plan = jr.normalize_plan(
        {
            "conditions": [
                {
                    "id": "a",
                    "label": "A",
                    "description": "d",
                    "actions": [
                        {"type": "takeover", "text": "stray", "instructions": "x"},
                        {"type": "agent_draft", "instructions": "  ", "tags": ["x"]},
                        {"type": "reply", "text": " hi ", "state": "closed"},
                    ],
                }
            ]
        }
    )
    takeover, draft, reply = plan.conditions[0].actions
    assert takeover.model_dump(exclude_none=True) == {"type": "takeover"}
    assert draft.model_dump(exclude_none=True) == {"type": "agent_draft"}
    assert reply.model_dump(exclude_none=True) == {"type": "reply", "text": "hi"}


def _cond(**over):
    return {"id": "a", "label": "A", "description": "d", "actions": [], **over}


@pytest.mark.parametrize(
    "data",
    [
        "not an object",
        {},
        {"conditions": []},
        {"conditions": "x"},
        {"conditions": [_cond(label="")]},
        {"conditions": [_cond(description="")]},
        {"conditions": [_cond(extra=1)]},
        {"conditions": [_cond(examples=["a", "b", "c", "d"])]},
        {"conditions": [_cond(examples=["x" * 301])]},
        {"conditions": [_cond(actions=[{"type": "nope"}])]},
        {"conditions": [_cond(actions=["takeover"])]},
        {"conditions": [_cond(actions=[{"type": "reply"}])]},
        {"conditions": [_cond(actions=[{"type": "set_state", "state": "muted"}])]},
        {"conditions": [_cond(actions=[{"type": "set_state"}])]},
        {"conditions": [_cond(actions=[{"type": "add_tags", "tags": []}])]},
        {"conditions": [_cond(actions=[{"type": "add_tags", "tags": list("abcdef")}])]},
        {"conditions": [_cond(actions=[{"type": "add_tags", "tags": ["x" * 41]}])]},
        {"conditions": [_cond(actions=[{"type": "takeover"}] * 6)]},
        {"conditions": [_cond()], "notes": ["n"] * 11},
        {"conditions": [_cond()], "unknown": 1},
    ],
)
def test_normalize_rejects_what_it_cannot_repair(env, data):
    with pytest.raises(env.core.errors.Invalid, match="invalid plan"):
        env.core.jev_rules.normalize_plan(data)


# --- prompt and JSON extraction ----------------------------------------------------------------


def test_prompt_embeds_the_document_verbatim_and_formats_nothing_else(env):
    jr = env.core.jev_rules
    doc = "Use {braces} and {document} and 100% of {0}"
    prompt = jr.build_prompt(doc)
    assert f"<<<\n{doc}\n>>>" in prompt
    assert prompt.count("{document}") == 1  # only the one inside the user's text
    assert '"id": "lowercase_snake_case, unique, at most 40 characters, never \\"else\\""' in prompt
    assert "that match none of the conditions above" in prompt
    assert "Il cliente chiede" in prompt


def test_extract_json_strips_fences_and_chatter(env):
    jr = env.core.jev_rules
    assert jr.extract_json('{"a": 1}') == {"a": 1}
    assert jr.extract_json('Sure!\n```json\n{"a": {"b": 2}}\n```\nbye') == {"a": {"b": 2}}
    with pytest.raises(ValueError):
        jr.extract_json("no json here")
    with pytest.raises(ValueError):
        jr.extract_json("{broken")


def test_extract_json_repairs_unescaped_quotes_inside_strings(env):
    jr = env.core.jev_rules
    raw = '{"description": "Solo un saluto, tipo "ok" oppure "grazie mille" e basta", "tags": ["a", "b"], "n": {"x": "y"}}'
    assert jr.extract_json(raw) == {
        "description": 'Solo un saluto, tipo "ok" oppure "grazie mille" e basta',
        "tags": ["a", "b"],
        "n": {"x": "y"},
    }


# --- the Hermes call --------------------------------------------------------------------------


def _fake_subprocess(env, *, returncode=0, stdout='{"x":1}', stderr="", raises=None):
    jr = env.core.jev_rules
    seen: dict[str, Any] = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["kw"] = list(argv), kw
        if raises is not None:
            raise raises
        usage = Path(argv[argv.index("--usage-file") + 1])
        seen["usage_path"] = usage
        usage.write_text(json.dumps({"model": "glm-x", "input_tokens": 5}), encoding="utf-8")
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)

    env.monkeypatch.setattr(jr.subprocess, "run", fake_run)
    env.monkeypatch.setattr(env.core.automations, "_hermes_bin", lambda: "/opt/hermes")
    return seen


def test_run_hermes_runs_the_one_shot_command_and_cleans_up(env):
    seen = _fake_subprocess(env)
    out, usage = env.real_run_hermes("PROMPT", 42)
    assert (out, usage) == ('{"x":1}', {"model": "glm-x", "input_tokens": 5})
    usage_path = seen["usage_path"]
    assert seen["argv"] == [
        "/opt/hermes", "-z", "PROMPT", "--reasoning", "none", "-t", "context_engine", "--ignore-rules",
        "--usage-file", str(usage_path),
    ]  # fmt: skip
    kw = seen["kw"]
    assert (kw["capture_output"], kw["text"], kw["timeout"], kw["check"]) == (True, True, 42, False)
    assert kw["cwd"] == env.core.db.data_dir()
    assert not usage_path.exists()


def test_run_hermes_failures_are_domain_errors_and_the_usage_file_is_removed(env):
    seen = _fake_subprocess(env, returncode=2, stderr="x" * 400 + "boom")
    with pytest.raises(env.core.errors.BadGateway, match=r"Hermes failed \(exit 2\): x+boom") as exc:
        env.real_run_hermes("p", 1)
    assert len(str(exc.value)) < 340
    assert not seen["usage_path"].exists()
    for failure in (FileNotFoundError("no hermes"), subprocess.TimeoutExpired("hermes", 1)):
        _fake_subprocess(env, raises=failure)
        with pytest.raises(env.core.errors.Unavailable, match="Hermes could not generate the plan"):
            env.real_run_hermes("p", 1)
    _fake_subprocess(env, stdout="")
    assert env.real_run_hermes("p", 1)[0] == ""


# --- generate ----------------------------------------------------------------------------------


def test_generate_returns_the_plan_and_scores_every_example(env):
    set_key(env)
    res = env.core.jev_rules.generate(env.conn, DOCUMENT)
    assert res["attempts"] == 1 and res["model"] == "glm-test" and isinstance(res["latency_ms"], int)
    assert res["plan"] == env.core.jev_rules.normalize_plan(PLAN).model_dump()
    assert res["check_error"] is None
    checks = {c["text"]: c for c in res["checks"]}
    assert len(checks) == 4
    hit = checks["voglio parlare con una persona"]
    assert (hit["expected"], hit["exit"], hit["score"], hit["ok"]) == ("person", "person", 0.95, True)
    miss = checks["sono molto deluso"]
    assert (miss["expected"], miss["exit"], miss["score"], miss["ok"]) == ("person", "else", None, False)
    assert miss["scores"] == {"person": 0.2, "agent": 0.1}
    assert checks["a che punto è la pratica?"]["ok"] and checks["ciao"]["expected"] == "else" and checks["ciao"]["ok"]
    assert env.prompts == [env.core.jev_rules.build_prompt(DOCUMENT)]
    assert len(env.posts) == 4 and {p["key"] for p in env.posts} == {KEY}
    for post in env.posts:  # the plan's conditions, not the stored ones
        assert list(post["body"]["questions"]) == ["person", "agent"]
        assert post["body"]["questions"]["agent"]["instructions"]["condition"] == "The client asks a routine question"
    # nothing is persisted
    assert env.core.settings.get_settings(env.conn).jev.exits == []
    assert env.conn.execute("SELECT COUNT(*) FROM automation_rules").fetchone()[0] == 0


def test_generate_retries_once_with_the_error_appended(env):
    env.hermes_outputs[:] = ["I cannot do that", json.dumps(PLAN)]
    res = env.core.jev_rules.generate(env.conn, DOCUMENT, check=False)
    assert res["attempts"] == 2
    first, second = env.prompts
    assert second.startswith(first) and "Your previous answer was not valid: the answer is not valid JSON" in second
    assert second.endswith("Return only the corrected JSON object.")
    env.prompts.clear()
    env.hermes_outputs[:] = [json.dumps({"conditions": []}), json.dumps(PLAN)]
    assert env.core.jev_rules.generate(env.conn, DOCUMENT, check=False)["attempts"] == 2
    assert "Your previous answer was not valid: invalid plan" in env.prompts[1]


def run_generate(env, body: dict[str, Any]) -> dict[str, Any]:
    """POST /jev/generate, then poll the job until it settles (the fake Hermes answers at once)."""
    r = env.client.post(f"{PREFIX}/jev/generate", json=body)
    assert r.status_code == 200, r.text
    job = r.json()
    deadline = time.monotonic() + 5
    while job["status"] == "running" and time.monotonic() < deadline:
        time.sleep(0.01)
        job = env.client.get(f"{PREFIX}/jev/generate/{job['job_id']}").json()
    return job


def test_two_invalid_answers_fail_the_job_with_502(env):
    env.hermes_outputs[:] = ["nope", json.dumps({"conditions": []})]
    job = run_generate(env, {"document": DOCUMENT})
    assert job["status"] == "failed" and job["error_status"] == 502
    assert "did not return a valid plan after 2 attempts" in job["error"] and "invalid plan" in job["error"]
    assert len(env.prompts) == 2


def test_generate_input_and_hermes_errors(env):
    jr, errors = env.core.jev_rules, env.core.errors
    for doc in ("", "  \n "):
        assert env.client.post(f"{PREFIX}/jev/generate", json={"document": doc}).status_code == 400
    with pytest.raises(errors.Invalid, match="longer than"):
        jr.generate(env.conn, "x" * 20001)
    assert env.client.post(f"{PREFIX}/jev/generate", json={"document": "x" * 20001}).status_code == 422
    assert env.client.post(f"{PREFIX}/jev/generate", json={"document": "x", "extra": 1}).status_code == 422
    assert env.prompts == []
    env.hermes_outputs.append(errors.Unavailable("Hermes could not generate the plan: gone"))
    job = run_generate(env, {"document": DOCUMENT})
    assert job["status"] == "failed" and job["error_status"] == 503 and "gone" in job["error"]
    env.hermes_outputs.append(errors.BadGateway("Hermes failed (exit 1): x"))
    assert run_generate(env, {"document": DOCUMENT})["error_status"] == 502
    assert env.client.get(f"{PREFIX}/jev/generate/unknown").status_code == 404


def test_generate_route_runs_in_the_background_and_check_can_be_skipped(env):
    set_key(env)
    job = run_generate(env, {"document": DOCUMENT, "check": False})
    assert job["status"] == "done"
    body = job["result"]
    assert body["checks"] is None and body["check_error"] is None and env.posts == []
    assert [c["id"] for c in body["plan"]["conditions"]] == ["person", "agent"]
    body = run_generate(env, {"document": DOCUMENT})["result"]
    assert len(body["checks"]) == 4 and len(env.posts) == 4


def test_generate_without_a_key_skips_the_check(env):
    res = env.core.jev_rules.generate(env.conn, DOCUMENT)
    assert res["checks"] is None and res["check_error"] == "No Jev API key: examples not checked"
    assert env.posts == []


def test_a_jev_failure_during_the_check_still_returns_the_plan(env):
    set_key(env)
    env.post_results.extend([(401, '{"detail":"bad key"}', {})] * 4)
    res = env.core.jev_rules.generate(env.conn, DOCUMENT)
    assert res["checks"] is None and "401" in res["check_error"] and "bad key" in res["check_error"]
    assert len(res["plan"]["conditions"]) == 2
    env.post_results.clear()  # the failed first check cancelled its pending calls
    env.post_results.extend([RuntimeError("Jev unreachable: down")] * 4)
    res = env.core.jev_rules.generate(env.conn, DOCUMENT)
    assert res["checks"] is None and "unreachable" in res["check_error"]


def test_a_plan_without_examples_has_empty_checks(env):
    set_key(env)
    plan = {"conditions": [{"id": "a", "label": "A", "description": "d"}]}
    env.hermes_outputs.append(json.dumps(plan))
    assert env.core.jev_rules.generate(env.conn, DOCUMENT)["checks"] == []
    assert env.posts == []


# --- plan -> exits and rules -----------------------------------------------------------------------

ALL_ACTIONS_PLAN = {
    "conditions": [
        {
            "id": "one",
            "label": "One",
            "description": "d1",
            "min_score": 0.8,
            "actions": [
                {"type": "takeover"},
                {"type": "escalate"},
                {"type": "set_state", "state": "closed"},
                {"type": "add_tags", "tags": ["a", "b"]},
                {"type": "reply", "text": "Thanks, we will call you"},
            ],
        },
        {
            "id": "two",
            "label": "Two",
            "description": "d2",
            "actions": [{"type": "agent_draft"}, {"type": "agent_reply", "instructions": "Be formal"}],
        },
    ],
    "else_actions": [{"type": "agent_draft", "instructions": "Ask one question"}],
}


def test_plan_to_exits_keeps_the_order_and_scores(env):
    plan = env.core.jev_rules.normalize_plan(PLAN)
    exits = env.core.jev_rules.plan_to_exits(plan)
    assert [(e.id, e.label, e.min_score) for e in exits] == [("person", "Needs a person", 0.6), ("agent", "Agent can answer", 0.7)]
    assert exits[1].description == "The client asks a routine question"


def test_every_action_type_becomes_a_valid_rule(env):
    jr = env.core.jev_rules
    rules = jr.plan_rules(jr.normalize_plan(ALL_ACTIONS_PLAN))
    assert [r["name"] for r in rules] == [
        "Jev · One → take over",
        "Jev · One → escalate",
        "Jev · One → set state closed",
        "Jev · One → tag a, b",
        "Jev · One → fixed reply",
        "Jev · Two → agent draft",
        "Jev · Two → agent reply",
        "Jev · Else → agent draft",
    ]
    tail = "\n\nWrite only the reply text."
    assert jr.AGENT_PROMPT.endswith(tail)
    expected = [
        ({"jev_exits": ["one"]}, {"type": "takeover"}, "none"),
        ({"jev_exits": ["one"]}, {"type": "escalate"}, "none"),
        ({"jev_exits": ["one"]}, {"type": "set_state", "state": "closed"}, "none"),
        ({"jev_exits": ["one"]}, {"type": "add_tags", "tags": ["a", "b"]}, "none"),
        ({"jev_exits": ["one"], "agent_active": True}, {"type": "reply", "text": "Thanks, we will call you"}, "send"),
        ({"jev_exits": ["two"], "agent_active": True}, {"type": "hermes", "prompt": jr.AGENT_PROMPT}, "draft"),
        (
            {"jev_exits": ["two"], "agent_active": True},
            {"type": "hermes", "prompt": jr.AGENT_PROMPT[: -len(tail)] + "\n\nInstructions: Be formal" + tail},
            "send",
        ),
        (
            {"jev_exits": ["else"], "agent_active": True},
            {"type": "hermes", "prompt": jr.AGENT_PROMPT[: -len(tail)] + "\n\nInstructions: Ask one question" + tail},
            "draft",
        ),
    ]
    for rule, (conditions, action, reply_mode) in zip(rules, expected, strict=True):
        assert rule["conditions"] == conditions and rule["action"] == action and rule["reply_mode"] == reply_mode
        assert rule["enabled"] is True and rule["account_id"] is None and rule["stop_after_match"] is False
        assert rule["event_types"] == ["conversation.classified"]
        env.core.automations_api.RuleIn.model_validate(rule)


def test_rule_names_are_capped_at_120_characters(env):
    jr = env.core.jev_rules
    plan = jr.normalize_plan(
        {
            "conditions": [
                {
                    "id": "a",
                    "label": "L" * 80,
                    "description": "d",
                    "actions": [{"type": "add_tags", "tags": ["t" * 40] * 5}],
                }
            ]
        }
    )
    (rule,) = jr.plan_rules(plan)
    assert len(rule["name"]) == 120
    env.core.automations_api.RuleIn.model_validate(rule)


# --- apply --------------------------------------------------------------------------------------


def managed(env) -> list[dict]:
    return [dict(r) for r in env.conn.execute("SELECT * FROM automation_rules WHERE managed_by = 'jev' ORDER BY position, id")]


def test_apply_stores_document_and_conditions_and_creates_managed_rules(env):
    cfg = env.client.get(f"{PREFIX}/settings").json()
    cfg["notifications"]["every_inbound"] = True
    cfg["jev"]["enabled"] = True
    cfg["jev"]["history_messages"] = 3
    assert env.client.put(f"{PREFIX}/settings", json=cfg).status_code == 200
    before = env.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'settings.updated'").fetchone()[0]

    r = apply_plan(env)
    assert r.status_code == 200, r.text
    body = r.json()
    assert [e["id"] for e in body["settings"]["jev"]["exits"]] == ["person", "agent"]
    assert body["settings"]["jev"]["document"] == DOCUMENT
    assert [x["name"] for x in body["rules"]] == [
        "Jev · Needs a person → take over",
        "Jev · Needs a person → escalate",
        "Jev · Agent can answer → agent draft",
        "Jev · Else → tag review, misc",
    ]
    assert all(x["managed_by"] == "jev" and x["enabled"] for x in body["rules"])

    saved = env.client.get(f"{PREFIX}/settings").json()
    assert saved == body["settings"]
    assert saved["notifications"]["every_inbound"] is True
    assert saved["jev"]["enabled"] is True and saved["jev"]["history_messages"] == 3
    assert env.conn.execute("SELECT COUNT(*) FROM events WHERE type = 'settings.updated'").fetchone()[0] == before + 1
    assert [r["managed_by"] for r in managed(env)] == ["jev"] * 4
    assert env.client.get(f"{PREFIX}/automations").json()["rules"][0]["managed_by"] == "jev"


def test_apply_replaces_only_managed_rules_and_their_queued_runs(env):
    user = mk_rule(env, {"type": "escalate"}, name="mine")
    assert user["managed_by"] is None
    apply_plan(env)
    old_ids = [r["id"] for r in managed(env)]
    user_run = env.conn.execute(
        "INSERT INTO automation_runs (rule_id, event_id, status, created_at) VALUES (?,1,'queued',?)", (user["id"], NOW)
    ).lastrowid
    queued = [
        env.conn.execute(
            "INSERT INTO automation_runs (rule_id, event_id, status, created_at) VALUES (?,1,'queued',?)", (old_ids[0], NOW)
        ).lastrowid,
    ]
    done = env.conn.execute(
        "INSERT INTO automation_runs (rule_id, event_id, status, created_at) VALUES (?,1,'done',?)", (old_ids[1], NOW)
    ).lastrowid

    second = {"conditions": [{"id": "other", "label": "Other", "description": "d", "actions": [{"type": "reply", "text": "Hi"}]}]}
    r = apply_plan(env, second, document="new rules")
    assert r.status_code == 200, r.text

    rules = env.client.get(f"{PREFIX}/automations").json()["rules"]
    assert [x["name"] for x in rules] == ["mine", "Jev · Other → fixed reply"]
    assert rules[0]["managed_by"] is None and rules[1]["managed_by"] == "jev"
    assert rules[1]["position"] > rules[0]["position"] and not set(old_ids) & {x["id"] for x in rules}
    run_ids = {row[0] for row in env.conn.execute("SELECT id FROM automation_runs")}
    assert user_run in run_ids and done in run_ids and not set(queued) & run_ids
    cfg = env.client.get(f"{PREFIX}/settings").json()["jev"]
    assert [e["id"] for e in cfg["exits"]] == ["other"] and cfg["document"] == "new rules"


def test_apply_accepts_a_generate_result_and_rejects_bad_input_without_touching_anything(env):
    generated = run_generate(env, {"document": DOCUMENT, "check": False})["result"]
    assert apply_plan(env, generated["plan"]).status_code == 200
    ids = [r["id"] for r in managed(env)]
    assert len(ids) == 4
    bad = apply_plan(env, {"conditions": []})
    assert bad.status_code == 400 and "invalid plan" in bad.json()["detail"]
    assert [r["id"] for r in managed(env)] == ids
    assert env.core.settings.get_settings(env.conn).jev.document == DOCUMENT
    r = env.client.post(f"{PREFIX}/jev/apply", json={"document": "x" * 20001, "plan": PLAN})
    assert r.status_code == 422
    assert env.client.post(f"{PREFIX}/jev/apply", json={"document": "x", "plan": "nope"}).status_code == 422
    assert env.core.jev_rules.apply(env.conn, "x", {"plan": PLAN}, NOW)["rules"]


def test_user_edits_keep_managed_by_until_the_next_apply(env):
    apply_plan(env)
    rule = managed(env)[0]
    body = {"name": "renamed", "action": {"type": "escalate"}, "event_types": ["conversation.classified"]}
    assert env.client.put(f"{PREFIX}/automations/{rule['id']}", json=body).json()["managed_by"] == "jev"


def test_status_lists_conditions_with_their_bound_rules(env):
    st = env.client.get(f"{PREFIX}/jev").json()
    assert st == {
        "enabled": False, "key_set": False, "key_source": None, "model": "jev-latest", "document": "",
        "conditions": [], "else": {"rules": []},
    }  # fmt: skip
    mine = mk_rule(env, {"type": "escalate"}, name="mine", enabled=False, conditions={"jev_exits": ["agent", "else"]})
    apply_plan(env)
    st = env.client.put(f"{PREFIX}/jev/key", json={"api_key": "abc"}).json()
    assert st["key_set"] is True and st["key_source"] == "settings" and "abc" not in json.dumps(st)
    assert st["document"] == DOCUMENT and st["model"] == "jev-latest"
    person, agent = st["conditions"]
    assert (person["id"], person["label"], person["min_score"]) == ("person", "Needs a person", 0.6)
    assert person["description"] == "The client is upset or asks to talk to a person"
    assert [(r["name"], r["enabled"], r["managed"]) for r in person["rules"]] == [
        ("Jev · Needs a person → take over", True, True),
        ("Jev · Needs a person → escalate", True, True),
    ]
    assert [(r["id"], r["name"], r["enabled"], r["managed"]) for r in agent["rules"]] == [
        (mine["id"], "mine", False, False),
        (agent["rules"][1]["id"], "Jev · Agent can answer → agent draft", True, True),
    ]
    assert [(r["name"], r["managed"]) for r in st["else"]["rules"]] == [
        ("mine", False),
        ("Jev · Else → tag review, misc", True),
    ]


# --- POST /jev/test -------------------------------------------------------------------------------


def test_test_scores_free_text_against_the_stored_conditions_and_persists_nothing(env):
    set_key(env)
    apply_plan(env)
    env.posts.clear()
    r = env.client.post(f"{PREFIX}/jev/test", json={"text": "voglio parlare con una persona"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["exit"], body["score"], body["model"]) == ("person", 0.95, "jev-test")
    assert body["summary"].startswith("Exit condition: Needs a person (score 0.95)")
    assert set(body) == {"state", "scores", "exit", "score", "summary", "model", "latency_ms", "usage"}
    assert body["state"] == {
        "number": "", "contact": {"name": None, "phone": None},
        "conversation": {"state": "new", "tags": [], "handled_by": "agent"},
        "latest_message": "voglio parlare con una persona",
        "recent_messages": ["contact: voglio parlare con una persona"],
    }  # fmt: skip
    assert len(env.posts) == 1 and list(env.posts[0]["body"]["questions"]) == ["person", "agent"]
    assert env.conn.execute("SELECT COUNT(*) FROM jev_runs").fetchone()[0] == 0
    assert env.client.post(f"{PREFIX}/jev/test", json={"text": "ciao"}).json()["exit"] == "else"


def test_test_conditions_override_the_stored_ones(env):
    set_key(env)
    override = [{"id": "x", "label": "X", "description": "anything", "min_score": 0.05}]
    r = env.client.post(f"{PREFIX}/jev/test", json={"text": "ciao", "conditions": override})
    assert r.status_code == 200, r.text  # works with no stored conditions at all
    assert r.json()["exit"] == "x" and list(env.posts[-1]["body"]["questions"]) == ["x"]
    apply_plan(env)
    r = env.client.post(f"{PREFIX}/jev/test", json={"text": "ciao", "conditions": override})
    assert list(env.posts[-1]["body"]["questions"]) == ["x"] and r.json()["exit"] == "x"
    conv = add_conv(env)
    r = env.client.post(f"{PREFIX}/jev/test", json={"conversation_id": conv, "conditions": override})
    assert r.status_code == 200
    assert env.posts[-1]["body"]["state"]["latest_message"] == "a che punto è la pratica?"
    assert env.posts[-1]["body"]["state"]["number"] == "Main"
    assert list(env.posts[-1]["body"]["questions"]) == ["x"]


def test_test_needs_exactly_one_source_a_key_and_conditions(env):
    url = f"{PREFIX}/jev/test"
    conv = add_conv(env)
    set_key(env)
    assert env.client.post(url, json={}).status_code == 400
    both = env.client.post(url, json={"text": "ciao", "conversation_id": conv})
    assert both.status_code == 400 and "exactly one" in both.json()["detail"]
    assert env.client.post(url, json={"text": ""}).status_code == 422
    assert env.client.post(url, json={"text": "x" * 2001}).status_code == 422
    r = env.client.post(url, json={"text": "ciao"})
    assert r.status_code == 400 and "no exit conditions" in r.json()["detail"]
    dup = {"id": "x", "label": "X", "description": "d"}
    r = env.client.post(url, json={"text": "ciao", "conditions": [dup, dup]})
    assert r.status_code == 400 and "invalid conditions" in r.json()["detail"]
    r = env.client.post(url, json={"text": "ciao", "conditions": [{**dup, "id": "else"}]})
    assert r.status_code == 400
    assert env.client.post(url, json={"text": "ciao", "conditions": [{**dup, "id": "Bad Id"}]}).status_code == 422
    assert env.client.post(url, json={"conversation_id": 999, "conditions": [dup]}).status_code == 404
    env.core.jev.set_api_key(env.conn, "", NOW)
    r = env.client.post(url, json={"text": "ciao", "conditions": [dup]})
    assert r.status_code == 400 and "no API key" in r.json()["detail"]
    assert env.posts == []
    set_key(env)
    env.post_results.append((429, "slow down", {}))
    r = env.client.post(url, json={"text": "ciao", "conditions": [dup]})
    assert r.status_code == 502 and "429" in r.json()["detail"]
    env.post_results.append(RuntimeError("Jev unreachable: down"))
    assert env.client.post(url, json={"text": "ciao", "conditions": [dup]}).status_code == 503


# --- CLI ---------------------------------------------------------------------------------------------


@pytest.fixture
def cli(env):
    mod = _load("hwc_wa_cli_jev_rules_test", CLI_FILE)
    env.monkeypatch.setattr(mod.core.jev, "_post", env.core.jev._post)
    env.monkeypatch.setattr(mod.core.jev_rules, "_run_hermes", env.core.jev_rules._run_hermes)

    return SimpleNamespace(main=mod.main)


def wa(cli, capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_cli_show_on_off(env, cli, capsys):
    code, out, _ = wa(cli, capsys, "jev", "show")
    assert code == 0
    assert "Jev: off  key: not set  model: jev-latest" in out and "(empty)" in out and "(none" in out
    assert wa(cli, capsys, "jev", "on")[1].strip() == "Jev is on"
    assert env.core.settings.get_settings(env.conn).jev.enabled is True
    assert "Jev: on" in wa(cli, capsys, "jev", "show")[1]
    assert json.loads(wa(cli, capsys, "jev", "off", "--json")[1]) == {"enabled": False}
    assert env.core.settings.get_settings(env.conn).jev.enabled is False


def test_cli_show_lists_conditions_and_bound_rules(env, cli, capsys):
    set_key(env)
    mk_rule(env, {"type": "escalate"}, name="mine", enabled=False, conditions={"jev_exits": ["person"]})
    apply_plan(env)
    code, out, _ = wa(cli, capsys, "jev", "show")
    assert code == 0
    assert "key: set (settings)" in out and "Rules document:" in out and "- If the client is upset" in out
    assert "1. Needs a person [person] min 0.6" in out and "2. Agent can answer [agent] min 0.7" in out
    assert "   The client is upset or asks to talk to a person" in out
    assert "- mine (off)" in out and "- Jev · Needs a person → take over (on, from rules)" in out
    assert "Else (no condition reached its minimum)" in out and "- Jev · Else → tag review, misc (on, from rules)" in out
    data = json.loads(wa(cli, capsys, "jev", "show", "--json")[1])
    assert [c["id"] for c in data["conditions"]] == ["person", "agent"] and data["key_set"] is True


def test_cli_generate_previews_without_storing(env, cli, capsys):
    set_key(env)
    rules = env.tmp_path / "rules.md"
    rules.write_text(DOCUMENT, encoding="utf-8")
    code, out, err = wa(cli, capsys, "jev", "generate", str(rules))
    assert code == 0, err
    assert "1. Needs a person [person] min 0.6" in out
    assert "   actions: take over; escalate" in out and "   actions: agent draft" in out
    assert "✓ voglio parlare con una persona -> person 0.95" in out
    assert "✗ sono molto deluso -> else" in out and "✓ ciao -> else" in out
    assert "Else (no condition reached its minimum)" in out and "   actions: tag review, misc" in out
    assert "Notes:" in out and "- Nothing said about opening hours" in out
    assert "<<<\n" + DOCUMENT in env.prompts[0]
    assert env.core.settings.get_settings(env.conn).jev.exits == [] and managed(env) == []


def test_cli_generate_without_key_or_check_and_with_stored_document(env, cli, capsys):
    code, out, err = wa(cli, capsys, "jev", "generate")  # no stored document
    assert code == 1 and out == "" and err.startswith("error: Write the Jev rules first")
    cfg = env.core.settings.get_settings(env.conn)
    env.core.settings.save_settings(env.conn, cfg.model_copy(update={"jev": cfg.jev.model_copy(update={"document": DOCUMENT})}), NOW)
    code, out, _ = wa(cli, capsys, "jev", "generate")  # stored document, no key
    assert code == 0 and "Check: No Jev API key: examples not checked" in out and "- voglio" in out
    set_key(env)
    code, out, _ = wa(cli, capsys, "jev", "generate", "--no-check", "--json")
    data = json.loads(out)
    assert code == 0 and data["checks"] is None and data["check_error"] is None and env.posts == []
    assert [c["id"] for c in data["plan"]["conditions"]] == ["person", "agent"]


def test_cli_generate_apply_reads_stdin_and_stores_everything(env, cli, capsys):
    set_key(env)
    env.monkeypatch.setattr(sys, "stdin", io.StringIO(DOCUMENT))
    code, out, err = wa(cli, capsys, "jev", "generate", "-", "--apply")
    assert code == 0, err
    assert "Applied 2 conditions and 4 rules." in out and "- Jev · Needs a person → take over (on)" in out
    assert env.core.settings.get_settings(env.conn).jev.document == DOCUMENT
    assert len(managed(env)) == 4
    code, out, _ = wa(cli, capsys, "jev", "generate", "--apply", "--json")  # stored document now
    data = json.loads(out)
    assert code == 0 and set(data) == {"generate", "apply"}
    assert len(data["generate"]["checks"]) == 4 and len(data["apply"]["rules"]) == 4
    assert len(managed(env)) == 4  # replaced, not duplicated


def test_cli_apply_takes_a_generate_result_or_a_bare_plan(env, cli, capsys):
    generated = env.tmp_path / "generated.json"
    generated.write_text(json.dumps({"plan": PLAN, "model": "x", "checks": None}), encoding="utf-8")
    rules = env.tmp_path / "rules.md"
    rules.write_text(DOCUMENT, encoding="utf-8")
    code, out, err = wa(cli, capsys, "jev", "apply", str(generated), "--rules", str(rules))
    assert code == 0, err
    assert "Applied 2 conditions and 4 rules." in out and "2. Agent can answer [agent] min 0.7" in out
    assert env.core.settings.get_settings(env.conn).jev.document == DOCUMENT
    env.monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(ALL_ACTIONS_PLAN)))
    code, out, _ = wa(cli, capsys, "jev", "apply", "-", "--json")  # document: the stored one
    data = json.loads(out)
    assert code == 0 and len(data["rules"]) == 8 and data["settings"]["jev"]["document"] == DOCUMENT
    assert len(managed(env)) == 8


def test_cli_apply_errors_exit_1(env, cli, capsys):
    code, out, err = wa(cli, capsys, "jev", "apply", str(env.tmp_path / "missing.json"))
    assert code == 1 and out == "" and err.startswith("error:")
    bad = env.tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert wa(cli, capsys, "jev", "apply", str(bad))[0] == 1
    bad.write_text(json.dumps({"conditions": []}), encoding="utf-8")
    code, _, err = wa(cli, capsys, "jev", "apply", str(bad))
    assert code == 1 and "invalid plan" in err
    assert managed(env) == []


def test_cli_test_scores_text_or_a_conversation(env, cli, capsys):
    set_key(env)
    apply_plan(env)
    code, out, err = wa(cli, capsys, "jev", "test", "a che punto è la pratica?")
    assert code == 0, err
    assert "Exit condition: Agent can answer (score 0.90)" in out and "(model jev-test," in out
    conv = add_conv(env, "voglio parlare con una persona")
    code, out, _ = wa(cli, capsys, "jev", "test", "--conversation", str(conv), "--json")
    data = json.loads(out)
    assert code == 0 and data["exit"] == "person" and data["score"] == 0.95
    assert wa(cli, capsys, "jev", "test")[0] == 1
    code, _, err = wa(cli, capsys, "jev", "test", "ciao", "--conversation", str(conv))
    assert code == 1 and "exactly one" in err
    assert wa(cli, capsys, "jev", "test", "--conversation", "999")[0] == 1
