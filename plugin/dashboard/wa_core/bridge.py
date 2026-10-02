"""HTTP client for one vendored Baileys bridge (loopback port per account)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class BridgeUnavailable(Exception):
    pass


def bridge_request(
    port: int, method: str, path: str, payload: dict | None = None, *, timeout: float
) -> tuple[int, Any]:
    """Return ``(http_status, parsed_json)``. Raises BridgeUnavailable when nothing answers."""
    # The bridge accepts only loopback Host headers.
    url = f"http://127.0.0.1:{int(port)}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, method=method, headers={"Content-Type": "application/json"} if data else {}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b"null")
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read() or b"null")
        except ValueError:
            return exc.code, None
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BridgeUnavailable(f"bridge at {url} unreachable ({exc})")
