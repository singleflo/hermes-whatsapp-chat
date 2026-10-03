"""HTTP routes for automation rules and runs (mounted without prefix by plugin_api)."""

import json
import re
import time
from contextlib import contextmanager
from typing import Annotated, Literal, Optional, Union

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import automations, db, errors

router = APIRouter()

State = Literal["new", "in_progress", "waiting", "muted", "closed"]
EventType = Literal[
    "message.in", "message.out", "conversation.created", "conversation.state_changed", "conversation.classified"
]
JEV_EXIT_ID = r"^[a-z][a-z0-9_]{0,39}$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Conditions(_Strict):
    states: Optional[list[State]] = None
    agent_active: Optional[bool] = None
    business_hours: Optional[Literal["in", "out"]] = None
    text_contains: Optional[list[str]] = None
    text_regex: Optional[str] = None
    tags_any: Optional[list[str]] = None
    first_message: Optional[bool] = None
    directions: Optional[list[Literal["in", "out"]]] = None
    jev_exits: Optional[list[Annotated[str, Field(pattern=JEV_EXIT_ID)]]] = None

    @field_validator("text_regex")
    @classmethod
    def _regex_compiles(cls, value):
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid regular expression: {exc}") from exc
        return value


class HermesAction(_Strict):
    type: Literal["hermes"]
    profile: Optional[str] = None
    prompt: str = Field(min_length=1)
    timeout_s: int = Field(default=180, ge=1, le=3600)
    skills: list[str] = Field(default_factory=list)


class WebhookAction(_Strict):
    type: Literal["webhook"]
    url: str = Field(pattern=r"^https?://\S+$")
    secret: Optional[str] = None
    timeout_s: int = Field(default=30, ge=1, le=3600)


class ScriptAction(_Strict):
    type: Literal["script"]
    command: list[str] = Field(min_length=1)
    timeout_s: int = Field(default=60, ge=1, le=3600)


class ReplyAction(_Strict):
    type: Literal["reply"]
    text: str = Field(min_length=1)


class SetStateAction(_Strict):
    type: Literal["set_state"]
    state: State


class AddTagsAction(_Strict):
    type: Literal["add_tags"]
    tags: list[str] = Field(min_length=1)


class EscalateAction(_Strict):
    type: Literal["escalate"]


class TakeoverAction(_Strict):
    type: Literal["takeover"]


Action = Annotated[
    Union[
        HermesAction,
        WebhookAction,
        ScriptAction,
        ReplyAction,
        SetStateAction,
        AddTagsAction,
        EscalateAction,
        TakeoverAction,
    ],
    Field(discriminator="type"),
]


class RuleIn(_Strict):
    name: str = Field(min_length=1, max_length=120)
    enabled: bool = True
    account_id: Optional[int] = None
    event_types: list[EventType] = Field(default_factory=lambda: list[EventType](["message.in"]), min_length=1)
    conditions: Conditions = Field(default_factory=Conditions)
    action: Action
    reply_mode: Literal["draft", "send", "none"] = "draft"
    stop_after_match: bool = False


class ReorderIn(_Strict):
    ids: list[int]


class TestIn(_Strict):
    conversation_id: int


@contextmanager
def _conn():
    conn = db.connect()
    try:
        yield conn
    except errors.WaError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    finally:
        conn.close()


def _rule(conn, rule_id: int) -> dict:
    row = conn.execute("SELECT * FROM automation_rules WHERE id = ?", (rule_id,)).fetchone()
    if row is None:
        raise errors.NotFound(f"automation rule {rule_id} not found")
    return automations.rule_to_dict(row)


def _rules(conn) -> list[dict]:
    rows = conn.execute("SELECT * FROM automation_rules ORDER BY position, id").fetchall()
    return [automations.rule_to_dict(r) for r in rows]


def _check_account(conn, account_id: Optional[int]) -> None:
    if account_id is not None and conn.execute("SELECT 1 FROM accounts WHERE id = ?", (account_id,)).fetchone() is None:
        raise errors.Invalid(f"account {account_id} not found")


def _columns(body: RuleIn) -> tuple:
    return (
        body.name.strip(),
        1 if body.enabled else 0,
        body.account_id,
        json.dumps(list(dict.fromkeys(body.event_types))),
        json.dumps(body.conditions.model_dump(exclude_none=True), ensure_ascii=False),
        json.dumps(body.action.model_dump(), ensure_ascii=False),
        body.reply_mode,
        1 if body.stop_after_match else 0,
    )


def _run(row) -> dict:
    return {
        "id": row["id"],
        "rule_id": row["rule_id"],
        "rule_name": row["rule_name"],
        "event_id": row["event_id"],
        "conversation_id": row["conversation_id"],
        "status": row["status"],
        "attempts": row["attempts"],
        "not_before": row["not_before"],
        "output": row["output"],
        "error": row["error"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
    }


_RUN_SQL = "SELECT r.*, u.name AS rule_name FROM automation_runs r LEFT JOIN automation_rules u ON u.id = r.rule_id"


def _get_run(conn, run_id: int) -> dict:
    row = conn.execute(_RUN_SQL + " WHERE r.id = ?", (run_id,)).fetchone()
    if row is None:
        raise errors.NotFound(f"automation run {run_id} not found")
    return _run(row)


# --- Rules ---------------------------------------------------------------------------


@router.get("/automations")
def list_rules():
    with _conn() as conn:
        return {"rules": _rules(conn)}


@router.post("/automations")
def create_rule(body: RuleIn):
    now = int(time.time())
    with _conn() as conn:
        _check_account(conn, body.account_id)
        name, enabled, account_id, event_types, conditions, action, reply_mode, stop = _columns(body)
        with db.write_txn(conn):
            position = conn.execute("SELECT COALESCE(MAX(position), 0) + 1 FROM automation_rules").fetchone()[0]
            cur = conn.execute(
                "INSERT INTO automation_rules (name, enabled, position, account_id, event_types, conditions, action,"
                " reply_mode, stop_after_match, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (name, enabled, position, account_id, event_types, conditions, action, reply_mode, stop, now, now),
            )
            rule_id = int(cur.lastrowid or 0)
        return _rule(conn, rule_id)


@router.post("/automations/reorder")
def reorder_rules(body: ReorderIn):
    now = int(time.time())
    with _conn() as conn:
        existing = [r["id"] for r in _rules(conn)]
        wanted = list(dict.fromkeys(body.ids))
        unknown = [i for i in wanted if i not in existing]
        if unknown:
            raise errors.NotFound(f"automation rule {unknown[0]} not found")
        order = wanted + [i for i in existing if i not in wanted]
        with db.write_txn(conn):
            for position, rule_id in enumerate(order, start=1):
                conn.execute(
                    "UPDATE automation_rules SET position = ?, updated_at = ? WHERE id = ? AND position != ?",
                    (position, now, rule_id, position),
                )
        return {"rules": _rules(conn)}


@router.put("/automations/{rule_id}")
def update_rule(rule_id: int, body: RuleIn):
    now = int(time.time())
    with _conn() as conn:
        _rule(conn, rule_id)
        _check_account(conn, body.account_id)
        name, enabled, account_id, event_types, conditions, action, reply_mode, stop = _columns(body)
        with db.write_txn(conn):
            conn.execute(
                "UPDATE automation_rules SET name = ?, enabled = ?, account_id = ?, event_types = ?, conditions = ?,"
                " action = ?, reply_mode = ?, stop_after_match = ?, updated_at = ? WHERE id = ?",
                (name, enabled, account_id, event_types, conditions, action, reply_mode, stop, now, rule_id),
            )
        return _rule(conn, rule_id)


@router.delete("/automations/{rule_id}")
def delete_rule(rule_id: int):
    with _conn() as conn:
        _rule(conn, rule_id)
        with db.write_txn(conn):
            conn.execute("DELETE FROM automation_rules WHERE id = ?", (rule_id,))
        return {"ok": True}


@router.post("/automations/{rule_id}/test")
def test_rule(rule_id: int, body: TestIn):
    with _conn() as conn:
        return automations.dry_run(conn, _rule(conn, rule_id), body.conversation_id, int(time.time()))


# --- Runs ----------------------------------------------------------------------------


@router.get("/automation-runs")
def list_runs(
    conversation_id: Optional[int] = None,
    rule_id: Optional[int] = None,
    limit: int = Query(50, ge=1, le=200),
):
    clauses, args = [], []
    if conversation_id is not None:
        clauses.append("r.conversation_id = ?")
        args.append(conversation_id)
    if rule_id is not None:
        clauses.append("r.rule_id = ?")
        args.append(rule_id)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with _conn() as conn:
        rows = conn.execute(_RUN_SQL + where + " ORDER BY r.id DESC LIMIT ?", (*args, limit)).fetchall()
        return {"runs": [_run(r) for r in rows]}


@router.post("/automation-runs/{run_id}/retry")
def retry_run(run_id: int):
    with _conn() as conn:
        run = _get_run(conn, run_id)
        if run["status"] == "running":
            raise errors.Conflict(f"automation run {run_id} is running")
        if run["status"] != "queued":
            with db.write_txn(conn):
                conn.execute(
                    "UPDATE automation_runs SET status = 'queued', attempts = 0, not_before = NULL, output = NULL,"
                    " error = NULL, started_at = NULL, finished_at = NULL WHERE id = ?",
                    (run_id,),
                )
        return _get_run(conn, run_id)
