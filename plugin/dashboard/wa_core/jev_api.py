"""HTTP routes for Jev exit conditions (mounted without prefix by plugin_api)."""

import time
from contextlib import contextmanager
from typing import Any, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import db, errors, jev, jev_rules, settings

router = APIRouter()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeyIn(_Strict):
    api_key: str = Field(max_length=500)


class GenerateIn(_Strict):
    document: str = Field(max_length=jev_rules.MAX_DOCUMENT_CHARS)
    check: bool = True


class ApplyIn(_Strict):
    document: str = Field(max_length=jev_rules.MAX_DOCUMENT_CHARS)
    plan: dict[str, Any]


class TestIn(_Strict):
    conversation_id: Optional[int] = None
    text: Optional[str] = Field(default=None, min_length=1, max_length=2000)
    conditions: Optional[list[settings.JevExit]] = Field(default=None, max_length=30)


@contextmanager
def _conn():
    conn = db.connect()
    try:
        yield conn
    except errors.WaError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    finally:
        conn.close()


@router.get("/jev")
def get_jev():
    with _conn() as conn:
        return jev_rules.status(conn)


@router.put("/jev/key")
def put_key(body: KeyIn):
    with _conn() as conn:
        jev.set_api_key(conn, body.api_key, int(time.time()))
        return jev_rules.status(conn)


@router.post("/jev/generate")
def generate(body: GenerateIn):
    with _conn() as conn:
        return jev_rules.generate(conn, body.document, check=body.check)


@router.post("/jev/apply")
def apply(body: ApplyIn):
    with _conn() as conn:
        return jev_rules.apply(conn, body.document, body.plan, int(time.time()))


@router.post("/jev/test")
def test_jev(body: TestIn):
    with _conn() as conn:
        if (body.conversation_id is None) == (body.text is None):
            raise errors.Invalid("Pass exactly one of conversation_id or text")
        if body.text is not None:
            return jev.score_text(conn, body.text, body.conditions)
        assert body.conversation_id is not None
        return jev.score_conversation(conn, body.conversation_id, body.conditions)


@router.post("/conversations/{conversation_id}/classify")
def classify(conversation_id: int):
    with _conn() as conn:
        with db.write_txn(conn):
            run_id = jev.enqueue_now(conn, conversation_id, int(time.time()))
        return {"queued": True, "run_id": run_id}
