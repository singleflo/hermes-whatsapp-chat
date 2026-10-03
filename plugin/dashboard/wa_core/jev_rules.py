"""Jev rules: one Markdown document -> LLM plan -> Jev exit conditions + generated automation rules.

``generate`` asks the default model of the Hermes profile (public CLI, one shot, no tools) to turn the
user's document into a *plan*; each example message of the plan is then scored by Jev (the "check").
``apply`` stores the document and the plan's conditions and replaces every automation rule with
``managed_by = 'jev'`` by rules generated from the plan's actions. Rules made by the user are never touched.

The Hermes call and the Jev calls run outside any database transaction; ``apply`` is one transaction.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from . import automations, automations_api, db, errors, jev, settings

GENERATE_TIMEOUT_S = 180
MAX_DOCUMENT_CHARS = 20000
MANAGED_BY = "jev"
CHECK_THREADS = 4
_ID_PATTERN = r"^[a-z][a-z0-9_]{0,39}$"
_ID_RE = re.compile(_ID_PATTERN)
_DEFAULT_MIN_SCORE = 0.7

ACTION_WORDS = {
    "takeover": "take over",
    "escalate": "escalate",
    "agent_draft": "agent draft",
    "agent_reply": "agent reply",
    "reply": "fixed reply",
    "set_state": "set state {state}",
    "add_tags": "tag {tags}",
}
AGENT_PROMPT = (
    "You are replying on WhatsApp on behalf of the account owner.\n"
    "Contact: {contact_name} ({phone}) on {account_label}\n"
    "Conversation state: {state}\n\n"
    "Recent messages:\n{history}\n\n"
    "Latest message: {text}\n\n"
    "Jev:\n{jev}\n\n"
    "Write only the reply text."
)
_AGENT_TAIL = "\n\nWrite only the reply text."

# Verbatim: `{document}` is replaced with str.replace, nothing else is formatted.
PROMPT = r"""You turn a business owner's WhatsApp rules, written in Markdown, into exit conditions for Jev, a classifier that scores each incoming WhatsApp message against every condition (the probability, 0 to 1, that the condition applies now). The first condition in list order whose score reaches its min_score is chosen; when none does, "else" is chosen. Then the actions of the chosen condition run.

Return ONLY one JSON object, no prose and no code fences, with this shape. Inside JSON strings never write a double quote character: quote words with single quotes, like 'ok'.
{
  "conditions": [
    {
      "id": "lowercase_snake_case, unique, at most 40 characters, never \"else\"",
      "label": "short name in the language of the rules, at most 60 characters",
      "description": "the condition, in the language of the rules, at most 400 characters",
      "min_score": 0.7,
      "actions": [ACTION, ...],
      "examples": ["two short realistic incoming messages, in the language of the rules, that must match this condition"]
    }
  ],
  "else_actions": [ACTION, ...],
  "else_examples": ["one or two short incoming messages that match none of the conditions above (check each against every condition)"],
  "notes": ["ambiguities or rules you could not map, in the language of the rules; empty list if none"]
}

ACTION is exactly one of:
{"type": "takeover"}  a person handles the conversation; the agent stops replying
{"type": "escalate"}  mark the conversation as urgent
{"type": "agent_draft", "instructions": "..."}  the Hermes agent writes a reply draft that a person approves
{"type": "agent_reply", "instructions": "..."}  the Hermes agent replies immediately (only when the rules explicitly ask for automatic replies)
{"type": "reply", "text": "..."}  send this fixed text immediately
{"type": "set_state", "state": "in_progress" | "waiting" | "closed"}
{"type": "add_tags", "tags": ["tag"]}

How to write a description (Jev reads it literally):
- One situation per condition, judged on the latest incoming message in the context of the recent messages.
- Concrete and self-contained: name the topics, requests or words that signal it, with short example phrases in single quotes.
- Present tense, about what the contact writes now ('The contact asks ...', 'Il cliente chiede ...'). No dates, times, counts or comparisons: Jev cannot compute them.
- Do not mention actions, other conditions or the owner's internal process.

Order, scores and actions:
- Keep the order of the rules, but put conditions that need a person (complaints, urgent or sensitive matters) before routine ones unless the rules say otherwise.
- min_score: 0.7 by default; 0.8 when the actions send a message, close the conversation or reply automatically; 0.6 when they only flag the conversation for a person.
- Cover every rule: each situation the rules describe becomes a condition (closely related bullets of one section may share a condition), and the rule about what happens when nothing else applies becomes else_actions. Never drop a rule, including rules whose answer is to do nothing or to close the conversation. Do not invent conditions the rules do not mention.
- Prefer agent_draft over agent_reply and reply unless the rules explicitly ask to answer automatically.
- Write descriptions, instructions, text, labels, examples and notes in the language of the rules.

The rules:
<<<
{document}
>>>"""


# --- Plan ---------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PlanAction(_Strict):
    type: Literal["takeover", "escalate", "agent_draft", "agent_reply", "reply", "set_state", "add_tags"]
    instructions: str | None = Field(default=None, max_length=1000)
    text: str | None = Field(default=None, min_length=1, max_length=1000)
    state: Literal["in_progress", "waiting", "closed"] | None = None
    tags: list[Annotated[str, Field(min_length=1, max_length=40)]] | None = Field(default=None, min_length=1, max_length=5)

    @model_validator(mode="after")
    def _fields_match_type(self) -> PlanAction:
        used = {
            "agent_draft": {"instructions"},
            "agent_reply": {"instructions"},
            "reply": {"text"},
            "set_state": {"state"},
            "add_tags": {"tags"},
        }.get(self.type, set())
        required = {"reply": "text", "set_state": "state", "add_tags": "tags"}.get(self.type)
        for field in ("instructions", "text", "state", "tags"):
            value = getattr(self, field)
            if field == required and value is None:
                raise ValueError(f"action {self.type} needs {field}")
            if field not in used and value is not None:
                raise ValueError(f"action {self.type} does not take {field}")
        return self


Example = Annotated[str, Field(max_length=300)]


class PlanCondition(_Strict):
    id: str = Field(pattern=_ID_PATTERN)
    label: str = Field(min_length=1, max_length=80)
    description: str = Field(min_length=1, max_length=500)
    min_score: float = Field(default=_DEFAULT_MIN_SCORE, ge=0, le=1)
    actions: list[PlanAction] = Field(default_factory=list, max_length=5)
    examples: list[Example] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def _not_else(self) -> PlanCondition:
        if self.id == jev.ELSE:
            raise ValueError('condition id "else" is reserved')
        return self


class Plan(_Strict):
    conditions: list[PlanCondition] = Field(min_length=1, max_length=30)
    else_actions: list[PlanAction] = Field(default_factory=list, max_length=5)
    else_examples: list[Example] = Field(default_factory=list, max_length=3)
    notes: list[Example] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _unique_ids(self) -> Plan:
        seen: set[str] = set()
        for c in self.conditions:
            if c.id in seen:
                raise ValueError(f"duplicate condition id {c.id!r}")
            seen.add(c.id)
        return self


_ACTION_KEYS = {
    "agent_draft": ("instructions",),
    "agent_reply": ("instructions",),
    "reply": ("text",),
    "set_state": ("state",),
    "add_tags": ("tags",),
}


def _slug(label: Any) -> str:
    folded = unicodedata.normalize("NFKD", label if isinstance(label, str) else "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "_", folded.lower()).strip("_") or "condition"
    if slug[0].isdigit():
        slug = "c_" + slug
    return slug[:40].rstrip("_") or "condition"


def _unique_id(base: str, used: set[str]) -> str:
    candidate, n = base, 2
    while candidate in used:
        suffix = f"_{n}"
        candidate = base[: 40 - len(suffix)] + suffix
        n += 1
    return candidate


def _strip_list(value: Any) -> Any:
    if not isinstance(value, list):
        return value
    out = [v.strip() if isinstance(v, str) else v for v in value]
    return [v for v in out if v != ""]


def _norm_action(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return raw
    kind = raw.get("type")
    kind = kind.strip() if isinstance(kind, str) else kind
    action: dict[str, Any] = {"type": kind}
    for key in _ACTION_KEYS.get(kind, ()) if isinstance(kind, str) else ():
        value = raw.get(key)
        if key == "tags":
            value = _strip_list(value)
        elif isinstance(value, str):
            value = value.strip()
        if value is None or value == "":
            continue
        action[key] = value
    return action


def _norm_actions(value: Any) -> Any:
    return [_norm_action(a) for a in value] if isinstance(value, list) else value


def _norm_score(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return _DEFAULT_MIN_SCORE
    return min(1.0, max(0.0, float(value)))


def _norm_condition(raw: Any, used: set[str]) -> Any:
    if not isinstance(raw, dict):
        return raw
    cond = dict(raw)
    for key in ("label", "description"):
        if isinstance(cond.get(key), str):
            cond[key] = cond[key].strip()
    if isinstance(cond.get("label"), str):
        cond["label"] = cond["label"][:80]
    if isinstance(cond.get("description"), str):
        cond["description"] = cond["description"][:500]
    ident = cond.get("id")
    ident = ident.strip() if isinstance(ident, str) else ident
    base = ident if isinstance(ident, str) and _ID_RE.match(ident) else _slug(cond.get("label"))
    cond["id"] = _unique_id(base, used)
    used.add(cond["id"])
    if "min_score" in cond:
        cond["min_score"] = _norm_score(cond["min_score"])
    if "actions" in cond:
        cond["actions"] = _norm_actions(cond["actions"])
    if "examples" in cond:
        cond["examples"] = _strip_list(cond["examples"])
    return cond


def _validation_text(exc: ValidationError) -> str:
    parts = []
    for item in exc.errors()[:5]:
        where = ".".join(str(p) for p in item["loc"])
        parts.append(f"{where}: {item['msg']}" if where else str(item["msg"]))
    return "; ".join(parts)[:500]


def normalize_plan(data: Any) -> Plan:
    """Validate a plan (bare or wrapped in a generate result), repairing what a model commonly gets slightly wrong."""
    if isinstance(data, dict) and isinstance(data.get("plan"), dict):
        data = data["plan"]
    if not isinstance(data, dict):
        raise errors.Invalid("invalid plan: expected a JSON object")
    plan = dict(data)
    if isinstance(plan.get("conditions"), list):
        used = {jev.ELSE}
        plan["conditions"] = [_norm_condition(c, used) for c in plan["conditions"]]
    for key in ("else_actions",):
        if key in plan:
            plan[key] = _norm_actions(plan[key])
    for key in ("else_examples", "notes"):
        if key in plan:
            plan[key] = _strip_list(plan[key])
    try:
        return Plan.model_validate(plan)
    except ValidationError as exc:
        raise errors.Invalid(f"invalid plan: {_validation_text(exc)}") from exc


# --- Hermes (generation) ---------------------------------------------------------------------


def build_prompt(document: str) -> str:
    return PROMPT.replace("{document}", document)


def _repair_quotes(text: str) -> str:
    """Escape double quotes the model left unescaped inside strings (`tipo "ok" oppure`).

    A quote inside a string closes it only when the next non-blank character can follow a JSON
    string (`:` `,` `}` `]` or the end); any other quote is content and gets escaped. Best effort:
    the caller still validates the result and retries the model when it does not parse.
    """
    out: list[str] = []
    in_string = escaped = False
    for i, ch in enumerate(text):
        if not in_string:
            in_string = ch == '"'
            out.append(ch)
            continue
        if escaped:
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == '"':
            rest = text[i + 1 :].lstrip()
            if not rest or rest[0] in ":,}]":
                in_string = False
            else:
                out.append('\\"')
                continue
        out.append(ch)
    return "".join(out)


def extract_json(text: str) -> Any:
    """The JSON object inside a model answer (code fences and chatter around it are ignored)."""
    cleaned = re.sub(r"^\s*```[\w-]*\s*$", "", text, flags=re.MULTILINE)
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("the answer contains no JSON object")
    candidate = cleaned[start : end + 1]
    try:
        return json.loads(candidate)
    except json.JSONDecodeError as exc:
        try:
            return json.loads(_repair_quotes(candidate))
        except json.JSONDecodeError:
            near = candidate[max(0, exc.pos - 60) : exc.pos + 20].replace("\n", " ")
            raise ValueError(f"{exc.msg} near: {near!r}") from exc


def _run_hermes(prompt: str, timeout: float) -> tuple[str, dict[str, Any]]:
    """Test seam: one shot of the profile's default model, no tools. Returns (stdout, usage)."""
    data = db.data_dir()
    data.mkdir(parents=True, exist_ok=True)
    fd, usage_path = tempfile.mkstemp(prefix="jev-usage-", suffix=".json")
    os.close(fd)
    argv = [
        automations._hermes_bin(),
        "-z",
        prompt,
        "--reasoning",
        "none",
        "-t",
        "context_engine",
        "--ignore-rules",
        "--usage-file",
        usage_path,
    ]
    try:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=data, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise errors.Unavailable(f"Hermes could not generate the plan: {exc}") from exc
        if proc.returncode != 0:
            tail = (proc.stderr or "").strip()[-300:]
            raise errors.BadGateway(f"Hermes failed (exit {proc.returncode}): {tail}")
        usage: dict[str, Any] = {}
        try:
            parsed = json.loads(Path(usage_path).read_text(encoding="utf-8") or "{}")
            if isinstance(parsed, dict):
                usage = parsed
        except (OSError, ValueError):
            pass
        return proc.stdout or "", usage
    finally:
        Path(usage_path).unlink(missing_ok=True)


def plan_to_exits(plan: Plan) -> list[settings.JevExit]:
    return [
        settings.JevExit(id=c.id, label=c.label, description=c.description, min_score=c.min_score)
        for c in plan.conditions
    ]


def _check(cfg: settings.JevSettings, key: str, plan: Plan) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Score every example of the plan with Jev; ``cfg``/``key`` were read beforehand (no database in threads)."""
    items = [(text, c.id) for c in plan.conditions for text in c.examples]
    items += [(text, jev.ELSE) for text in plan.else_examples]
    if not items:
        return [], None

    def one(item: tuple[str, str]) -> dict[str, Any]:
        text, expected = item
        res = jev.score_state(cfg, key, jev.text_state(text))
        return {
            "text": text,
            "expected": expected,
            "exit": res["exit"],
            "score": res["score"],
            "scores": res["scores"],
            "ok": res["exit"] == expected,
        }

    try:
        with ThreadPoolExecutor(max_workers=CHECK_THREADS) as pool:
            return list(pool.map(one, items)), None
    except errors.WaError as exc:
        return None, str(exc)


def generate(conn, document: str, *, check: bool = True) -> dict[str, Any]:
    """Turn the rules document into a plan (nothing is stored). Two attempts, then 502."""
    if not (document or "").strip():
        raise errors.Invalid("Write the Jev rules first")
    if len(document) > MAX_DOCUMENT_CHARS:
        raise errors.Invalid(f"The Jev rules are longer than {MAX_DOCUMENT_CHARS} characters")
    cfg = settings.get_settings(conn).jev
    key = jev.api_key(conn)[0]
    base_prompt = build_prompt(document)
    prompt, started = base_prompt, time.monotonic()
    plan: Plan | None = None
    usage: dict[str, Any] = {}
    problem = ""
    attempts = 0
    for attempts in (1, 2):
        stdout, usage = _run_hermes(prompt, GENERATE_TIMEOUT_S)
        try:
            plan = normalize_plan(extract_json(stdout))
            break
        except ValueError as exc:
            problem = f"the answer is not valid JSON ({exc})"
        except errors.Invalid as exc:
            problem = str(exc)
        prompt = base_prompt + f"\n\nYour previous answer was not valid: {problem}. Return only the corrected JSON object."
    if plan is None:
        raise errors.BadGateway(f"Hermes did not return a valid plan after 2 attempts: {problem}")
    latency_ms = int((time.monotonic() - started) * 1000)
    checks, check_error = None, None
    if check:
        if key is None:
            check_error = "No Jev API key: examples not checked"
        else:
            checks, check_error = _check(cfg.model_copy(update={"exits": plan_to_exits(plan)}), key, plan)
    return {
        "plan": plan.model_dump(),
        "model": usage.get("model"),
        "latency_ms": latency_ms,
        "attempts": attempts,
        "checks": checks,
        "check_error": check_error,
    }


# --- Rules --------------------------------------------------------------------------------


def action_words(action: PlanAction) -> str:
    return ACTION_WORDS[action.type].format(state=action.state, tags=", ".join(action.tags or []))


def _rule(label: str, exit_id: str, action: PlanAction) -> dict[str, Any]:
    conditions: dict[str, Any] = {"jev_exits": [exit_id]}
    reply_mode = "none"
    body: dict[str, Any]
    if action.type == "takeover":
        body = {"type": "takeover"}
    elif action.type == "escalate":
        body = {"type": "escalate"}
    elif action.type == "set_state":
        body = {"type": "set_state", "state": action.state}
    elif action.type == "add_tags":
        body = {"type": "add_tags", "tags": list(action.tags or [])}
    elif action.type == "reply":
        body, reply_mode = {"type": "reply", "text": action.text}, "send"
        conditions["agent_active"] = True
    else:  # agent_draft / agent_reply
        prompt = AGENT_PROMPT
        if action.instructions:
            prompt = prompt.replace(_AGENT_TAIL, "\n\nInstructions: " + action.instructions + _AGENT_TAIL)
        body = {"type": "hermes", "prompt": prompt}
        reply_mode = "draft" if action.type == "agent_draft" else "send"
        conditions["agent_active"] = True
    return {
        "name": f"Jev · {label} → {action_words(action)}"[:120],
        "enabled": True,
        "account_id": None,
        "event_types": ["conversation.classified"],
        "conditions": conditions,
        "action": body,
        "reply_mode": reply_mode,
        "stop_after_match": False,
    }


def plan_rules(plan: Plan) -> list[dict[str, Any]]:
    """One RuleIn-shaped dict per action: the conditions in order, then Else."""
    rules = [_rule(c.label, c.id, a) for c in plan.conditions for a in c.actions]
    rules += [_rule("Else", jev.ELSE, a) for a in plan.else_actions]
    return rules


def apply(conn, document: str, plan_data: Any, now: int) -> dict[str, Any]:
    """Store the document and the plan's conditions and replace every Jev-managed rule, in one transaction."""
    if len(document) > MAX_DOCUMENT_CHARS:
        raise errors.Invalid(f"The Jev rules are longer than {MAX_DOCUMENT_CHARS} characters")
    plan = normalize_plan(plan_data)
    try:
        rules = [automations_api.RuleIn.model_validate(r) for r in plan_rules(plan)]
    except ValidationError as exc:
        raise errors.Invalid(f"invalid plan: {_validation_text(exc)}") from exc
    exits = plan_to_exits(plan)
    with db.write_txn(conn):
        current = settings.get_settings(conn)
        updated = current.model_copy(update={"jev": current.jev.model_copy(update={"document": document, "exits": exits})})
        saved = settings.write_settings(conn, updated, now)
        old = f"SELECT id FROM automation_rules WHERE managed_by = '{MANAGED_BY}'"
        conn.execute(f"DELETE FROM automation_runs WHERE status = 'queued' AND rule_id IN ({old})")
        conn.execute("DELETE FROM automation_rules WHERE managed_by = ?", (MANAGED_BY,))
        position = int(conn.execute("SELECT COALESCE(MAX(position), 0) FROM automation_rules").fetchone()[0])
        for rule in rules:
            position += 1
            name, enabled, account_id, event_types, conditions, action, reply_mode, stop = automations_api._columns(rule)
            conn.execute(
                "INSERT INTO automation_rules (name, enabled, position, account_id, event_types, conditions, action,"
                " reply_mode, stop_after_match, created_at, updated_at, managed_by) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (name, enabled, position, account_id, event_types, conditions, action, reply_mode, stop, now, now, MANAGED_BY),
            )
        rows = conn.execute(
            "SELECT * FROM automation_rules WHERE managed_by = ? ORDER BY position, id", (MANAGED_BY,)
        ).fetchall()
    return {"settings": saved.model_dump(), "rules": [automations.rule_to_dict(r) for r in rows]}


def status(conn) -> dict[str, Any]:
    """Jev as the UI and CLI show it: key, document, ordered conditions and the rules bound to each (and to Else)."""
    cfg = settings.get_settings(conn).jev
    rows = conn.execute("SELECT * FROM automation_rules ORDER BY position, id").fetchall()
    bound: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        wanted = db.jloads(row["conditions"], {}).get("jev_exits") or []
        for exit_id in wanted if isinstance(wanted, list) else []:
            bound.setdefault(exit_id, []).append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "enabled": bool(row["enabled"]),
                    "managed": row["managed_by"] == MANAGED_BY,
                }
            )
    return {
        "enabled": cfg.enabled,
        **jev.key_status(conn),
        "model": cfg.model,
        "document": cfg.document,
        "conditions": [
            {
                "id": e.id,
                "label": e.label,
                "description": e.description,
                "min_score": e.min_score,
                "rules": bound.get(e.id, []),
            }
            for e in cfg.exits
        ],
        "else": {"rules": bound.get(jev.ELSE, [])},
    }
