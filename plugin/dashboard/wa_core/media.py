"""Message media: meta parsing, listing for the API, data URLs with path containment."""

from __future__ import annotations

import base64
import mimetypes
import sqlite3
from pathlib import Path
from typing import Any

from . import db, errors

MAX_DATA_URL_BYTES = 15 * 1024 * 1024


def media_entries(paths: Any) -> list[dict[str, Any]]:
    """Meta ``media`` entries for bridge-cached files: ``{path, mime, name, size}``."""
    out: list[dict[str, Any]] = []
    if not isinstance(paths, list):
        return out
    for raw in paths:
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw)
        try:
            size = path.stat().st_size
        except OSError:
            size = None
        out.append({"path": raw, "mime": mimetypes.guess_type(raw)[0], "name": path.name, "size": size})
    return out


def allowed_roots() -> list[Path]:
    root = db.data_dir()
    return [(root / "wa-media").resolve(), (root / "uploads").resolve()]


def _resolve_contained(raw: str) -> Path | None:
    """Resolved path of ``raw`` when it lies under an allowed root and is a file, else None."""
    try:
        resolved = Path(raw).resolve()
    except (OSError, RuntimeError, ValueError):
        return None
    for root in allowed_roots():
        if resolved.is_relative_to(root):
            return resolved if resolved.is_file() else None
    return None


def message_media(meta_raw: str | None) -> list[dict[str, Any]]:
    """API view of a message's media: ``[{index, type, mime, name, size, available}]``."""
    meta = db.jloads(meta_raw, None)
    if not isinstance(meta, dict):
        return []
    items = meta.get("media")
    if not isinstance(items, list):
        return []
    media_type = meta.get("mediaType")
    out: list[dict[str, Any]] = []
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        out.append(
            {
                "index": index,
                "type": media_type,
                "mime": item.get("mime"),
                "name": item.get("name"),
                "size": item.get("size"),
                "available": _resolve_contained(str(item.get("path") or "")) is not None,
            }
        )
    return out


def media_data_url(conn: sqlite3.Connection, message_id: int, index: int) -> dict[str, Any]:
    row = conn.execute("SELECT meta FROM messages WHERE id = ?", (message_id,)).fetchone()
    if row is None:
        raise errors.NotFound(f"message {message_id} not found")
    meta = db.jloads(row["meta"], None)
    items = meta.get("media") if isinstance(meta, dict) else None
    if not isinstance(items, list) or not 0 <= index < len(items) or not isinstance(items[index], dict):
        raise errors.NotFound(f"message {message_id} has no media #{index}")
    item = items[index]
    path = _resolve_contained(str(item.get("path") or ""))
    if path is None:
        raise errors.NotFound("media file is not available")
    size = path.stat().st_size
    if size > MAX_DATA_URL_BYTES:
        raise errors.TooLarge(f"media is {size} bytes; the inline limit is {MAX_DATA_URL_BYTES}")
    mime = item.get("mime") or mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return {"data_url": f"data:{mime};base64,{encoded}", "mime": mime, "name": item.get("name") or path.name, "size": size}
