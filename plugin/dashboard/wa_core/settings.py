"""Plugin settings: one validated JSON object stored under key ``global``."""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import db

SETTINGS_KEY = "global"
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _check_hhmm(value: str) -> str:
    if not isinstance(value, str) or not _HHMM.match(value):
        raise ValueError(f"expected HH:MM, got {value!r}")
    return value


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Rules(_Strict):
    inbound_on_closed: Literal["reopen_new", "reopen_previous", "keep"] = "reopen_new"
    inbound_on_waiting: Literal["in_progress", "keep"] = "in_progress"
    inbound_on_muted: Literal["reopen_new", "keep"] = "reopen_new"
    outbound_on_active: Literal["waiting", "keep"] = "waiting"
    auto_reply_window_seconds: int = Field(default=10, ge=0, le=300)
    auto_close_waiting_days: int | None = Field(default=7, ge=1, le=365)


class QuietHours(_Strict):
    start: str
    end: str

    @field_validator("start", "end")
    @classmethod
    def _hhmm(cls, value: str) -> str:
        return _check_hhmm(value)


class Notifications(_Strict):
    new_conversation: bool = True
    every_inbound: bool = False
    escalation: bool = True
    quiet_hours: QuietHours | None = None


class Board(_Strict):
    urgency_hours: int = Field(default=24, ge=1, le=24 * 365)
    mute_presets_hours: list[int] = Field(default_factory=lambda: [1, 8, 24, 168], max_length=12)
    drop_mute_hours: int = Field(default=24, ge=1, le=24 * 365)
    closed_limit: int = Field(default=50, ge=1, le=500)

    @field_validator("mute_presets_hours")
    @classmethod
    def _presets(cls, value: list[int]) -> list[int]:
        if any(h < 1 or h > 24 * 365 for h in value):
            raise ValueError("mute presets must be between 1 and 8760 hours")
        return value


def _default_business() -> dict[str, list[list[str]]]:
    return {d: ([["09:00", "18:00"]] if d not in ("sat", "sun") else []) for d in DAYS}


class Hours(_Strict):
    timezone: str = "Europe/Rome"
    business: dict[str, list[list[str]]] = Field(default_factory=_default_business)

    @field_validator("timezone")
    @classmethod
    def _tz(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("business")
    @classmethod
    def _business(cls, value: dict[str, list[list[str]]]) -> dict[str, list[list[str]]]:
        out: dict[str, list[list[str]]] = {d: [] for d in DAYS}
        for day, spans in value.items():
            if day not in DAYS:
                raise ValueError(f"unknown weekday {day!r}")
            for span in spans:
                if len(span) != 2:
                    raise ValueError("each business-hours span is [start, end]")
                start, end = _check_hhmm(span[0]), _check_hhmm(span[1])
                if start >= end:
                    raise ValueError(f"span start must be before end ({start}-{end})")
                out[day].append([start, end])
        return out


class AutomationsSettings(_Strict):
    enabled: bool = True
    max_runs_per_conversation_per_hour: int = Field(default=10, ge=1, le=1000)
    history_messages_in_prompt: int = Field(default=20, ge=0, le=200)


class MediaSettings(_Strict):
    max_upload_mb: int = Field(default=15, ge=1, le=50)


class Privacy(_Strict):
    send_read_receipts: bool = True


class Settings(_Strict):
    rules: Rules = Field(default_factory=lambda: Rules())
    notifications: Notifications = Field(default_factory=lambda: Notifications())
    board: Board = Field(default_factory=lambda: Board())
    hours: Hours = Field(default_factory=lambda: Hours())
    automations: AutomationsSettings = Field(default_factory=lambda: AutomationsSettings())
    media: MediaSettings = Field(default_factory=lambda: MediaSettings())
    privacy: Privacy = Field(default_factory=lambda: Privacy())


def get_settings(conn: sqlite3.Connection) -> Settings:
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (SETTINGS_KEY,)).fetchone()
    if row is None:
        return Settings()
    try:
        return Settings.model_validate(db.jloads(row["value"], {}))
    except ValueError:
        return Settings()


def save_settings(conn: sqlite3.Connection, s: Settings, now: int) -> Settings:
    from . import events  # lazy: events pulls in the automations slice, which imports this module

    s = Settings.model_validate(s.model_dump())
    with db.write_txn(conn):
        conn.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (SETTINGS_KEY, json.dumps(s.model_dump()), now),
        )
        events.emit(conn, "settings.updated", now=now)
    return s
