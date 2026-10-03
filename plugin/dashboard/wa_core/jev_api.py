"""HTTP routes for Jev exit conditions (mounted without prefix by plugin_api)."""

import time
from contextlib import contextmanager

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from . import db, errors, jev

router = APIRouter()


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class KeyIn(_Strict):
    api_key: str = Field(max_length=500)


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


@router.get("/jev")
def get_jev():
    with _conn() as conn:
        return jev.key_status(conn)


@router.put("/jev/key")
def put_key(body: KeyIn):
    with _conn() as conn:
        jev.set_api_key(conn, body.api_key, int(time.time()))
        return jev.key_status(conn)


@router.post("/jev/test")
def test_jev(body: TestIn):
    with _conn() as conn:
        return jev.score_conversation(conn, body.conversation_id)


@router.post("/conversations/{conversation_id}/classify")
def classify(conversation_id: int):
    with _conn() as conn:
        with db.write_txn(conn):
            run_id = jev.enqueue_now(conn, conversation_id, int(time.time()))
        return {"queued": True, "run_id": run_id}
