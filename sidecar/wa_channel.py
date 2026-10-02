"""Independent WhatsApp channel for hermes-whatsapp-chat.

Runs the vendored Baileys bridge on its own port/session and feeds every message
into the plugin's DB. Never touches Hermes' native WhatsApp.

    uv run python sidecar/wa_channel.py pair        # scan the QR with the dedicated number
    uv run python sidecar/wa_channel.py install     # LaunchAgent: start now and at login
    uv run python sidecar/wa_channel.py uninstall
    uv run python sidecar/wa_channel.py run         # what launchd runs
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import plistlib
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import NoReturn

REPO = Path(__file__).resolve().parents[1]
BRIDGE_DIR = REPO / "sidecar" / "whatsapp-bridge"
LABEL = "it.fl1.hermes-whatsapp-chat.channel"
PLIST = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
POLL_SECONDS = 1.0
RESTART_DELAY_SECONDS = 5.0
MAX_PENDING = 1000


def _load_api():
    spec = importlib.util.spec_from_file_location("hwc_plugin_api", REPO / "plugin" / "dashboard" / "plugin_api.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


api = _load_api()


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def die(msg: str) -> NoReturn:
    print(msg, file=sys.stderr)
    sys.exit(1)


def bridge_env(data_dir: Path) -> dict[str, str]:
    # `*` admits every DM and every owner-typed chat; groups are never forwarded.
    media = data_dir / "wa-media"
    return {
        **os.environ,
        "WHATSAPP_MODE": "bot",
        "WHATSAPP_DM_POLICY": "allowlist",
        "WHATSAPP_ALLOWED_USERS": "*",
        "WHATSAPP_GROUP_POLICY": "disabled",
        "WHATSAPP_FORWARD_OWNER_MESSAGES": "true",
        "HERMES_IMAGE_CACHE_DIR": str(media / "image"),
        "HERMES_DOCUMENT_CACHE_DIR": str(media / "document"),
        "HERMES_AUDIO_CACHE_DIR": str(media / "audio"),
    }


def bridge_cmd(node: str, data_dir: Path, *extra: str) -> list[str]:
    return [node, str(BRIDGE_DIR / "bridge.js"), "--session", str(data_dir / "wa-session"), "--mode", "bot", *extra]


def find_node() -> str:
    node = os.environ.get("WA_NODE") or shutil.which("node")
    if not node:
        die("node not found (install Node 22+ or set WA_NODE)")
    if not (BRIDGE_DIR / "node_modules").exists():
        die("missing bridge deps: run npm ci --prefix sidecar/whatsapp-bridge")
    return node


def paired(data_dir: Path) -> bool:
    return (data_dir / "wa-session" / "creds.json").exists()


def cmd_pair(node: str, data_dir: Path) -> int:
    if paired(data_dir):
        print(f"already paired (delete {data_dir / 'wa-session'} to re-pair)")
        return 0
    return subprocess.run(bridge_cmd(node, data_dir, "--pair-only"), env=bridge_env(data_dir), cwd=BRIDGE_DIR).returncode


def drain(pending: list[dict]) -> None:
    """Ingest pending events; on a DB error keep the rest (the bridge queue drains on read)."""
    with closing(api._conn()) as conn:
        while pending:
            event = pending[0]
            try:
                api.ingest_event(conn, event, int(time.time()))
            except sqlite3.Error as exc:
                log(f"db error, keeping {len(pending)} events pending: {exc}")
                return
            except Exception as exc:
                log(f"dropping malformed event {event.get('messageId')}: {exc}")
            pending.pop(0)


def cmd_run(node: str, data_dir: Path) -> int:
    if not paired(data_dir):
        die("not paired: run uv run python sidecar/wa_channel.py pair")
    stop = False

    def request_stop(*_: object) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)

    cmd = bridge_cmd(node, data_dir, "--port", str(api._bridge_port()))
    bridge_log = open(data_dir / "bridge.log", "ab")

    def spawn() -> subprocess.Popen:
        return subprocess.Popen(cmd, env=bridge_env(data_dir), cwd=BRIDGE_DIR, stdout=bridge_log, stderr=subprocess.STDOUT)

    proc = spawn()
    pending: list[dict] = []
    while not stop:
        if proc.poll() is not None:
            log(f"bridge exited with code {proc.returncode}; restarting in {RESTART_DELAY_SECONDS:.0f}s")
            time.sleep(RESTART_DELAY_SECONDS)
            if stop:
                break
            proc = spawn()
            continue
        try:
            status, events = api._bridge_request("GET", "/messages", timeout=5)
            if status == 200 and isinstance(events, list):
                pending.extend(events)
        except api.BridgeUnavailable:
            pass
        if len(pending) > MAX_PENDING:
            log(f"dropping {len(pending) - MAX_PENDING} oldest pending events")
            del pending[: len(pending) - MAX_PENDING]
        drain(pending)
        time.sleep(POLL_SECONDS)

    proc.terminate()
    try:
        proc.wait(10)
    except subprocess.TimeoutExpired:
        proc.kill()
    return 0


def cmd_install(node: str, data_dir: Path) -> int:
    if not paired(data_dir):
        die("not paired: run uv run python sidecar/wa_channel.py pair")
    env = {"WA_NODE": node}
    env.update({k: os.environ[k] for k in ("WA_ARCHIVE_DB", "WA_BRIDGE_PORT") if k in os.environ})
    plist = {
        "Label": LABEL,
        "ProgramArguments": [sys.executable, str(Path(__file__).resolve()), "run"],
        "EnvironmentVariables": env,
        "WorkingDirectory": str(REPO),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(data_dir / "channel.log"),
        "StandardErrorPath": str(data_dir / "channel.log"),
    }
    PLIST.parent.mkdir(parents=True, exist_ok=True)
    PLIST.write_bytes(plistlib.dumps(plist))
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=False)
    subprocess.run(["launchctl", "bootstrap", domain, str(PLIST)], check=True)
    print(f"installed {PLIST}")
    return 0


def cmd_uninstall() -> int:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"], check=False)
    PLIST.unlink(missing_ok=True)
    print("uninstalled")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["pair", "run", "install", "uninstall"])
    command = parser.parse_args().command
    if command == "uninstall":
        return cmd_uninstall()
    node = find_node()
    data_dir = api._data_dir()
    return {"pair": cmd_pair, "run": cmd_run, "install": cmd_install}[command](node, data_dir)


if __name__ == "__main__":
    sys.exit(main())
