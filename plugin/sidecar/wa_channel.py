"""Independent WhatsApp channel (supervisor) for hermes-whatsapp-chat.

One LaunchAgent supervises one vendored Baileys bridge per WhatsApp account. The SQLite DB
is the control plane: the UI writes the desired state, this service reconciles it every
second and writes status, QR codes and a heartbeat back. Never touches Hermes' native WhatsApp.
The bridge's npm dependencies are installed on first start (npm ci, log in logs/npm.log).

    wa_channel.py install        # LaunchAgent: start now and at login (same as the plugin UI)
    wa_channel.py uninstall
    wa_channel.py status         # service + accounts
    wa_channel.py install-skill  # render the Hermes skill into <hermes home>/skills/
    wa_channel.py run            # what launchd runs

Run it with the plugin backend's Python (<data>/bin/hwc-python). Pairing (QR) happens in
the plugin UI, not here.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import queue
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from typing import IO, Any, NoReturn

PLUGIN_DIR = Path(__file__).resolve().parents[1]
BRIDGE_DIR = PLUGIN_DIR / "sidecar" / "whatsapp-bridge"
VENDOR_DIR = PLUGIN_DIR / "sidecar" / "_vendor"
VERSION = "2.0.0"
MIN_NODE_MAJOR = 20
DEPS_ERROR = "Bridge dependencies failed to install (see logs/npm.log)"
DEPS_RETRY_SECONDS = 30.0  # short: a plugin reinstall/update replaces the bridge dir under us
NPM_TIMEOUT_SECONDS = 1200.0

sys.path.insert(0, str(VENDOR_DIR))  # vendored segno (QR rendering)

TICK_SECONDS = 1.0
RESTART_DELAY_SECONDS = 5.0
STOP_GRACE_SECONDS = 10.0
UNREACHABLE_ERROR_AFTER_SECONDS = 20.0
PAIR_TIMEOUT_SECONDS = 600.0  # total time a QR is offered before pairing is parked
PAIR_STALE_SECONDS = 60.0  # no QR event from the pairing process for this long: restart it
PAIR_EXIT_GRACE_SECONDS = 15.0  # pairing process lingering after "connected"
TIMERS_EVERY_SECONDS = 30.0
MAX_PENDING = 5000
HISTORY_INGEST_PER_TICK = 300
HTTP_TIMEOUT = 5.0
AUTOMATION_THREADS = 2
CODE_CHECK_SECONDS = 5.0
PLUGIN_GONE_SECONDS = 120.0  # plugin.yaml missing this long = removed (shorter = a reinstall in progress)


def _load_api():
    spec = importlib.util.spec_from_file_location("hwc_plugin_api", PLUGIN_DIR / "dashboard" / "plugin_api.py")
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


api = _load_api()
core = api.core

# Precomputed so the self-uninstall still works after the plugin dir (and its code) is gone.
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{core.service.LABEL}.plist"
BOOTOUT_TARGET = f"gui/{os.getuid()}/{core.service.LABEL}"


def code_fingerprint(plugin_dir: Path) -> tuple[Any, ...] | None:
    """Identity of the plugin code on disk: (plugin.yaml hash, wa_channel.py mtime, wa_core dir + file
    mtimes). None when ``plugin.yaml`` (or any watched file) is missing, e.g. mid-reinstall."""
    try:
        manifest = hashlib.sha256((plugin_dir / "plugin.yaml").read_bytes()).hexdigest()
        channel = (plugin_dir / "sidecar" / "wa_channel.py").stat().st_mtime_ns
        core_dir = plugin_dir / "dashboard" / "wa_core"
        files = tuple(
            (p.name, p.stat().st_mtime_ns) for p in sorted(core_dir.iterdir()) if p.is_file()
        )
        return (manifest, channel, core_dir.stat().st_mtime_ns, files)
    except OSError:
        return None


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def die(msg: str) -> NoReturn:
    print(msg, file=sys.stderr)
    sys.exit(1)


# --- preconditions ---------------------------------------------------------------------


def check_node() -> tuple[str | None, str | None]:
    """Return (node path, error). The error is a one-line, UI-friendly message."""
    node = core.service.find_node()
    if not node:
        return None, f"node not found: install Node {MIN_NODE_MAJOR}+ or set WA_NODE"
    try:
        out = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10, check=False).stdout.strip()
    except (OSError, subprocess.SubprocessError) as exc:
        return node, f"cannot run node at {node}: {exc}"
    match = re.match(r"v(\d+)\.", out)
    if not match or int(match.group(1)) < MIN_NODE_MAJOR:
        return node, f"Node {out or 'unknown version'} at {node} is too old: Node {MIN_NODE_MAJOR}+ required (set WA_NODE)"
    return node, None


def bridge_deps_ready() -> bool:
    # npm writes this lockfile last, so it distinguishes a finished install from a partial one.
    return (BRIDGE_DIR / "node_modules" / ".package-lock.json").is_file()


def install_bridge_deps(node: str) -> bool:
    """`npm ci` in the bridge dir with the npm next to the node binary; output goes to logs/npm.log."""
    node_bin = Path(node).resolve().parent
    sibling = node_bin / "npm"
    npm = str(sibling) if sibling.is_file() else shutil.which("npm")
    logs = core.db.data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "PATH": os.pathsep.join([str(node_bin), os.environ.get("PATH", "")])}
    returncode: int | None = None
    with open(logs / "npm.log", "a", encoding="utf-8") as out:
        out.write(f"\n=== {time.strftime('%Y-%m-%d %H:%M:%S')} npm ci in {BRIDGE_DIR} ===\n")
        out.flush()
        if npm is None:
            out.write("npm not found\n")
        else:
            try:
                returncode = subprocess.run(
                    [npm, "ci", "--omit=dev", "--no-audit", "--no-fund"],
                    cwd=BRIDGE_DIR,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    timeout=NPM_TIMEOUT_SECONDS,
                    check=False,
                ).returncode
            except (OSError, subprocess.SubprocessError) as exc:
                out.write(f"npm failed: {exc}\n")
            else:
                out.write(f"npm exited with {returncode}\n")
    if returncode != 0 or not bridge_deps_ready():
        shutil.rmtree(BRIDGE_DIR / "node_modules", ignore_errors=True)
        return False
    return True


def qr_svg(qr: str) -> str | None:
    """Theme-friendly inline SVG (dark modules follow the text colour, transparent background)."""
    try:
        import segno
    except ImportError:
        log(f"vendored segno missing in {VENDOR_DIR}: QR codes cannot be rendered")
        return None
    # segno validates colours, so render black and swap it for currentColor afterwards.
    svg = segno.make(qr, error="l").svg_inline(scale=4, dark="#000", light=None)
    return svg.replace('"#000"', '"currentColor"')


# --- per-account runner ----------------------------------------------------------------

_UNSET: Any = object()


def bridge_env(account: dict) -> dict[str, str]:
    # `*` admits every DM and every owner-typed chat; groups are never forwarded.
    media = core.accounts.media_dir(account)
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
        "WHATSAPP_SYNC_HISTORY": str(account.get("history_mode") or "recent"),
    }


def _read_pair_stdout(stream: IO[str], events: queue.Queue) -> None:
    for line in stream:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.put(event)


class Runner:
    """Process state of one WhatsApp account: pairing subprocess or bridge, pending events."""

    def __init__(self, account_id: int, supervisor: Supervisor) -> None:
        self.id = account_id
        self.sup = supervisor
        self.proc: subprocess.Popen | None = None
        self.mode: str | None = None  # 'pair' | 'bridge' (mode of self.proc)
        self.dying: list[tuple[subprocess.Popen, float]] = []
        self.handled_restart: Any = _UNSET
        self.next_start_at = 0.0
        self.spawned_at = 0.0
        self.log_path: Path | None = None
        self.log_offset = 0
        self.error: str | None = None
        # bridge
        self.unreachable_since: float | None = None
        self.history_supported = True
        self.live_pending: deque[dict] = deque()
        self.history_pending: deque[dict] = deque()
        # pairing
        self.events: queue.Queue = queue.Queue()
        self.qr: str | None = None
        self.qr_svg: str | None = None
        self.last_qr_at: float | None = None
        self.pair_origin: float | None = None
        self.pair_connected_at: float | None = None
        self.pair_parked = False

    # -- process management --

    def _reap(self, mono: float) -> None:
        alive = []
        for proc, deadline in self.dying:
            if proc.poll() is None:
                if mono > deadline:
                    proc.kill()
                alive.append((proc, deadline))
        self.dying = alive

    def stop(self, mono: float) -> None:
        """Ask the current process to exit; it is reaped (then killed) in later ticks."""
        proc = self.proc
        self.proc = None
        self.mode = None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            self.dying.append((proc, mono + STOP_GRACE_SECONDS))

    def _spawn(self, cmd: list[str], account: dict, *, pipe_stdout: bool) -> subprocess.Popen:
        logs = core.db.data_dir() / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        log_path = logs / f"bridge-{self.id}.log"
        self.log_path = log_path
        self.log_offset = log_path.stat().st_size if log_path.exists() else 0
        core.accounts.session_path(account).mkdir(parents=True, exist_ok=True)
        media = core.accounts.media_dir(account)
        for sub in ("image", "document", "audio"):
            (media / sub).mkdir(parents=True, exist_ok=True)
        with open(log_path, "ab") as logfile:
            return subprocess.Popen(
                cmd,
                env=bridge_env(account),
                cwd=BRIDGE_DIR,
                stdout=subprocess.PIPE if pipe_stdout else logfile,
                stderr=logfile,
                text=pipe_stdout or None,
                encoding="utf-8" if pipe_stdout else None,
                bufsize=1 if pipe_stdout else -1,
            )

    def _log_tail(self) -> str:
        if self.log_path is None:
            return ""
        try:
            with open(self.log_path, "rb") as fh:
                fh.seek(self.log_offset)
                return fh.read(65536).decode("utf-8", "replace")
        except OSError:
            return ""

    # -- ingest --

    def _ingest(self, conn: sqlite3.Connection, pending: deque[dict], limit: int) -> None:
        done = 0
        while pending and done < limit:
            event = pending[0]
            try:
                if not isinstance(event, dict):
                    raise ValueError(f"not an object: {type(event).__name__}")
                core.ingest.ingest_event(
                    conn, self.id, event, int(time.time()), source="history" if event.get("history") else "live"
                )
            except sqlite3.Error as exc:
                log(f"account {self.id}: db error, keeping {len(pending)} events pending: {exc}")
                return
            except Exception as exc:
                mid = event.get("messageId") if isinstance(event, dict) else None
                log(f"account {self.id}: dropping malformed event {mid}: {exc}")
            pending.popleft()
            done += 1

    def drain(self, conn: sqlite3.Connection) -> None:
        self._ingest(conn, self.live_pending, len(self.live_pending))
        self._ingest(conn, self.history_pending, HISTORY_INGEST_PER_TICK)

    def _extend(self, pending: deque[dict], events: list, label: str) -> None:
        pending.extend(events)
        overflow = len(pending) - MAX_PENDING
        if overflow > 0:
            log(f"account {self.id}: dropping {overflow} oldest pending {label} events")
            for _ in range(overflow):
                pending.popleft()

    # -- reconcile --

    def step(self, conn: sqlite3.Connection, account: dict, mono: float, now: int) -> dict | None:
        """Reconcile one account. Returns the status to record, or None when the account is gone."""
        self._reap(mono)
        desired = account["desired"]
        if desired == "removed":
            self.stop(mono)
            self.live_pending.clear()
            self.history_pending.clear()
            if self.dying:
                return {"state": "stopped"}
            self._wipe_session(account)
            core.accounts.finalize_removed(conn, self.id)
            return None
        self.drain(conn)
        if desired != "running":
            self.stop(mono)
            self._reset_pairing()
            self.error = None
            if desired == "logged_out":
                if not self.dying:
                    self._wipe_session(account)
                return {"state": "logged_out"}
            return {"state": "stopped"}

        if self.sup.precondition_error:
            self.stop(mono)
            return {"state": "error", "error": self.sup.precondition_error}
        if self.sup.installing_deps:
            self.stop(mono)
            return {"state": "starting"}
        if account.get("port") is None:
            return {"state": "error", "error": "account has no bridge port"}

        restart = account.get("restart_requested_at")
        if self.handled_restart is _UNSET:
            self.handled_restart = restart
        elif restart != self.handled_restart:
            self.handled_restart = restart
            log(f"account {self.id}: restart requested")
            self.stop(mono)
            self._reset_pairing()
            self.error = None
            self.next_start_at = 0.0
            self.unreachable_since = None

        paired = core.accounts.is_paired(account)
        if self.mode == "pair" and self.proc is not None:
            return self._step_pair(conn, account, mono, now)
        if not paired:
            if self.mode == "bridge":
                self.stop(mono)
            return self._step_pair(conn, account, mono, now)
        return self._step_bridge(conn, account, mono)

    def _wipe_session(self, account: dict) -> None:
        path = core.accounts.session_path(account)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            log(f"account {self.id}: deleted session {path}")

    def _reset_pairing(self) -> None:
        self.qr = self.qr_svg = self.last_qr_at = None
        self.pair_origin = self.pair_connected_at = None
        self.pair_parked = False

    # -- pairing --

    def _consume_pair_events(self, conn: sqlite3.Connection, mono: float, now: int) -> None:
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                return
            kind = event.get("event")
            qr = event.get("qr")
            if kind == "qr" and isinstance(qr, str):
                self.qr = qr
                self.qr_svg = qr_svg(qr)
                self.last_qr_at = mono
            elif kind == "connected":
                user = event.get("user") if isinstance(event.get("user"), dict) else {}
                jid = user.get("id")
                if jid:
                    core.accounts.record_paired(conn, self.id, wa_jid=jid, wa_name=user.get("name"), now=now)
                log(f"account {self.id}: paired as {jid}")
                self.pair_connected_at = mono
                self.qr = self.qr_svg = None
                self.error = None
            elif kind == "error":
                if event.get("error") == "logged_out":
                    log(f"account {self.id}: WhatsApp rejected the session (logged out)")
                    core.accounts.set_desired(conn, self.id, "logged_out", now=now)
                    self.stop(mono)
                else:
                    self.error = str(event.get("error") or "pairing error")
                    log(f"account {self.id}: pairing error: {self.error}")

    def _pair_status(self) -> dict:
        if self.pair_connected_at is not None:
            return {"state": "connecting"}
        if self.qr:
            return {"state": "qr", "qr": self.qr, "qr_svg": self.qr_svg}
        if self.error:
            return {"state": "error", "error": self.error}
        return {"state": "pairing"}

    def _step_pair(self, conn: sqlite3.Connection, account: dict, mono: float, now: int) -> dict:
        if self.pair_parked:
            return {"state": "error", "error": self.error}
        if self.proc is None:
            if self.dying or mono < self.next_start_at:
                return self._pair_status() if self.error else {"state": "pairing"}
            if self.pair_origin is None:
                self.pair_origin = mono
            elif mono - self.pair_origin > PAIR_TIMEOUT_SECONDS:
                self.pair_parked = True
                self.error = "Pairing timed out: restart the account to show a new QR code"
                self._reset_qr()
                log(f"account {self.id}: pairing timed out")
                return {"state": "error", "error": self.error}
            node = self.sup.node
            assert node
            cmd = [
                node,
                str(BRIDGE_DIR / "bridge.js"),
                "--pair-only",
                "--pair-json",
                "--session",
                str(core.accounts.session_path(account)),
                "--mode",
                "bot",
            ]
            self.events = queue.Queue()
            self.qr = self.qr_svg = self.last_qr_at = self.pair_connected_at = None
            self.proc = self._spawn(cmd, account, pipe_stdout=True)
            self.mode = "pair"
            self.spawned_at = mono
            assert self.proc.stdout is not None
            threading.Thread(target=_read_pair_stdout, args=(self.proc.stdout, self.events), daemon=True).start()
            log(f"account {self.id}: pairing started (pid {self.proc.pid})")
            return {"state": "pairing", "pid": self.proc.pid}

        proc = self.proc
        self._consume_pair_events(conn, mono, now)
        if self.proc is not proc:  # a logged_out event stopped the process
            return {"state": "logged_out"}
        code = proc.poll()
        if code is not None:
            # the reader thread may still hold the tail of stdout
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                before = self.events.qsize()
                time.sleep(0.05)
                if self.events.qsize() == before:
                    break
            self._consume_pair_events(conn, mono, now)
            if self.proc is not proc:  # handled a logged_out event (stopped)
                return {"state": "logged_out"}
            self.proc = None
            self.mode = None
            self.pair_connected_at = None
            if core.accounts.is_paired(account) and code == 0:
                log(f"account {self.id}: pairing finished")
                return {"state": "connecting"}
            self.error = self.error or f"pairing process exited with code {code}"
            log(f"account {self.id}: {self.error}; retrying in {RESTART_DELAY_SECONDS:.0f}s")
            self.next_start_at = mono + RESTART_DELAY_SECONDS
            self._reset_qr()
            return {"state": "error", "error": self.error}

        if self.pair_connected_at is not None:
            if mono - self.pair_connected_at > PAIR_EXIT_GRACE_SECONDS:
                log(f"account {self.id}: pairing process did not exit, killing it")
                proc.kill()
            return {"state": "connecting", "pid": proc.pid}
        if mono - (self.last_qr_at or self.spawned_at) > PAIR_STALE_SECONDS:
            log(f"account {self.id}: pairing stalled, restarting it")
            self.stop(mono)
            self._reset_qr()
            self.next_start_at = 0.0
            return {"state": "pairing"}
        status = self._pair_status()
        status["pid"] = proc.pid
        return status

    def _reset_qr(self) -> None:
        self.qr = self.qr_svg = self.last_qr_at = None

    # -- bridge --

    def _step_bridge(self, conn: sqlite3.Connection, account: dict, mono: float) -> dict:
        if self.proc is not None:
            code = self.proc.poll()
            if code is not None and self._bridge_exited(conn, code, mono):
                return {"state": "logged_out"}
        if self.proc is None:
            if self.dying or mono < self.next_start_at:
                return {"state": "error", "error": self.error} if self.error else {"state": "starting"}
            node = self.sup.node
            assert node
            cmd = [
                node,
                str(BRIDGE_DIR / "bridge.js"),
                "--session",
                str(core.accounts.session_path(account)),
                "--mode",
                "bot",
                "--port",
                str(account["port"]),
            ]
            self.proc = self._spawn(cmd, account, pipe_stdout=False)
            self.mode = "bridge"
            self.spawned_at = mono
            self.unreachable_since = mono
            self.history_supported = True
            log(f"account {self.id}: bridge started on port {account['port']} (pid {self.proc.pid})")
            return {"state": "starting", "pid": self.proc.pid, "error": self.error}
        return self._poll_bridge(account, mono)

    def _bridge_exited(self, conn: sqlite3.Connection, code: int, mono: float) -> bool:
        """Record a bridge exit. Returns True when WhatsApp logged the number out."""
        self.proc = None
        self.mode = None
        # the bridge exits on a WhatsApp logout; restarting it would loop forever
        if "Logged out" in self._log_tail():
            log(f"account {self.id}: bridge reports logged out (exit {code})")
            core.accounts.set_desired(conn, self.id, "logged_out", now=int(time.time()))
            self.error = None
            return True
        self.error = f"bridge exited with code {code}"
        log(f"account {self.id}: {self.error}; restarting in {RESTART_DELAY_SECONDS:.0f}s")
        self.next_start_at = mono + RESTART_DELAY_SECONDS
        return False

    def _poll_bridge(self, account: dict, mono: float) -> dict:
        port = int(account["port"])
        proc = self.proc
        assert proc is not None
        try:
            status, events = core.bridge.bridge_request(port, "GET", "/messages", timeout=HTTP_TIMEOUT)
            if status == 200 and isinstance(events, list):
                self._extend(self.live_pending, events, "live")
            if self.history_supported:
                status, events = core.bridge.bridge_request(port, "GET", "/history?limit=1000", timeout=HTTP_TIMEOUT)
                if status == 404:
                    self.history_supported = False  # unpatched bridge: no history endpoint
                elif status == 200 and isinstance(events, list):
                    self._extend(self.history_pending, events, "history")
            status, health = core.bridge.bridge_request(port, "GET", "/health", timeout=HTTP_TIMEOUT)
        except (core.bridge.BridgeUnavailable, OSError):
            if self.unreachable_since is None:
                self.unreachable_since = mono
            if mono - self.unreachable_since < UNREACHABLE_ERROR_AFTER_SECONDS:
                return {"state": "starting", "pid": proc.pid, "error": self.error}
            return {"state": "error", "pid": proc.pid, "error": f"Bridge not responding on port {port}"}
        self.unreachable_since = None
        self.error = None
        connected = status == 200 and isinstance(health, dict) and health.get("status") == "connected"
        return {"state": "connected" if connected else "disconnected", "pid": proc.pid}


# --- supervisor ------------------------------------------------------------------------


class Supervisor:
    def __init__(self) -> None:
        self.node, self.node_error = check_node()
        self.deps_error: str | None = None
        self.deps_thread: threading.Thread | None = None
        self.deps_failed_at: float | None = None
        self.runners: dict[int, Runner] = {}
        self.stop_event = threading.Event()
        self.started_at = int(time.time())
        self.pool = ThreadPoolExecutor(max_workers=AUTOMATION_THREADS, thread_name_prefix="automations")
        self.slots: list[Future | None] = [None] * AUTOMATION_THREADS
        self.last_timers = 0.0
        self.last_precondition_check = time.monotonic()
        self.code_fp = code_fingerprint(PLUGIN_DIR)
        self.code_missing_since: float | None = None
        self.exit_action: str | None = None
        self.conn: sqlite3.Connection | None = None
        self._update_deps(self.last_precondition_check)

    @property
    def precondition_error(self) -> str | None:
        return self.node_error or self.deps_error

    @property
    def installing_deps(self) -> bool:
        return self.deps_thread is not None and self.deps_thread.is_alive()

    def _deps_worker(self, node: str) -> None:
        if install_bridge_deps(node):
            log("bridge dependencies installed")
            return
        self.deps_failed_at = time.monotonic()
        self.deps_error = DEPS_ERROR
        log(DEPS_ERROR)

    def _update_deps(self, mono: float) -> None:
        """Install the bridge's npm dependencies in the background when they are missing."""
        if self.node_error or not self.node:
            return
        if bridge_deps_ready():
            self.deps_error = None
            return
        if self.installing_deps:
            return
        if self.deps_failed_at is not None and mono - self.deps_failed_at < DEPS_RETRY_SECONDS:
            return
        self.deps_error = None
        self.deps_failed_at = None
        log("installing bridge dependencies (npm ci)")
        self.deps_thread = threading.Thread(target=self._deps_worker, args=(self.node,), name="npm-ci", daemon=True)
        self.deps_thread.start()

    def request_stop(self, *_: object) -> None:
        self.stop_event.set()

    def _connection(self) -> sqlite3.Connection:
        conn = self.conn
        if conn is None:
            conn = self.conn = core.db.connect()
        return conn

    def _drop_connection(self) -> None:
        if self.conn is not None:
            try:
                self.conn.close()
            except sqlite3.Error:
                pass
            self.conn = None

    def _automations(self) -> None:
        for i, slot in enumerate(self.slots):
            if slot is not None:
                if not slot.done():
                    continue
                exc = slot.exception()
                if exc is not None:
                    log(f"automations: worker failed: {exc!r}")
            self.slots[i] = self.pool.submit(core.automations.process_due, core.db.connect, int(time.time()))

    def tick(self) -> None:
        mono = time.monotonic()
        now = int(time.time())
        conn = self._connection()
        core.accounts.service_heartbeat(conn, pid=os.getpid(), version=VERSION, started_at=self.started_at, now=now)

        if self.node_error and mono - self.last_precondition_check > 30:
            self.last_precondition_check = mono
            self.node, self.node_error = check_node()
            if self.node_error is None:
                log("node found")
        self._update_deps(mono)

        rows = [dict(r) for r in conn.execute("SELECT * FROM accounts WHERE kind = 'whatsapp' ORDER BY id")]
        seen = {row["id"] for row in rows}
        for account_id in [i for i in self.runners if i not in seen]:
            runner = self.runners.pop(account_id)
            runner.stop(mono)
            self._finish([runner])
        for account in rows:
            runner = self.runners.get(account["id"])
            if runner is None:
                runner = self.runners[account["id"]] = Runner(account["id"], self)
            try:
                status = runner.step(conn, account, mono, now)
            except sqlite3.Error:
                raise
            except Exception as exc:
                log(f"account {account['id']}: reconcile failed: {exc!r}")
                status = {"state": "error", "error": f"sidecar error: {exc}"}
            if status is None:
                self.runners.pop(account["id"], None)
                continue
            core.accounts.set_status(conn, account["id"], now=now, **status)

        if mono - self.last_timers >= TIMERS_EVERY_SECONDS:
            self.last_timers = mono
            try:
                core.conversations.run_timers(conn, now)
            except sqlite3.Error as exc:
                log(f"timers: db error: {exc}")
        self._automations()

    def _finish(self, runners: list[Runner]) -> None:
        """Terminate every child of the given runners, killing after the grace period."""
        procs = [p for r in runners for p in [r.proc, *(d[0] for d in r.dying)] if p is not None]
        for proc in procs:
            if proc.poll() is None:
                proc.terminate()
        deadline = time.monotonic() + STOP_GRACE_SECONDS
        for proc in procs:
            try:
                proc.wait(max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def _code_check(self, mono: float) -> bool:
        """Watch the plugin code on disk (updates and reinstalls replace the dir under this process).

        Returns True when the loop must stop; ``self.exit_action`` says why (``restart``: new code is
        there, launchd's KeepAlive starts it; ``uninstall``: the plugin stayed removed)."""
        fp = code_fingerprint(PLUGIN_DIR)
        if fp is None:
            if self.code_missing_since is None:
                self.code_missing_since = mono
                log(f"plugin.yaml missing, waiting up to {PLUGIN_GONE_SECONDS:.0f} s for a reinstall")
            elif mono - self.code_missing_since >= PLUGIN_GONE_SECONDS:
                log("plugin removed, uninstalling service")
                self.exit_action = "uninstall"
                return True
            return False
        reappeared = self.code_missing_since is not None
        self.code_missing_since = None
        if reappeared or fp != self.code_fp:
            log("plugin code changed, restarting")
            self.exit_action = "restart"
            return True
        return False

    def _uninstall_self(self) -> None:
        """The plugin was removed: delete the LaunchAgent plist and unload the service (kills this process,
        so it is the last action). Plugin data is never touched. Uses only precomputed paths/values."""
        try:
            PLIST_PATH.unlink(missing_ok=True)
        except OSError as exc:
            log(f"could not delete {PLIST_PATH}: {exc}")
        try:
            subprocess.run(["launchctl", "bootout", BOOTOUT_TARGET], capture_output=True, timeout=30, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            log(f"launchctl bootout failed: {exc}")

    def run(self) -> int:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        log(f"channel {VERSION} started (pid {os.getpid()}, db {core.db.db_path()})")
        if self.precondition_error:
            log(f"PRECONDITION FAILED: {self.precondition_error}")
        last_code_check = time.monotonic()
        while not self.stop_event.is_set():
            started = time.monotonic()
            try:
                self.tick()
            except sqlite3.Error as exc:
                log(f"db error: {exc}")
                self._drop_connection()
            except Exception as exc:
                log(f"tick failed: {exc!r}")
            if started - last_code_check >= CODE_CHECK_SECONDS:
                last_code_check = started
                try:
                    if self._code_check(started):
                        self.stop_event.set()
                        break
                except Exception as exc:
                    log(f"code check failed: {exc!r}")
            self.stop_event.wait(max(0.0, TICK_SECONDS - (time.monotonic() - started)))
        log("stopping")
        self.pool.shutdown(wait=False, cancel_futures=True)
        runners = list(self.runners.values())
        for runner in runners:
            if runner.proc is not None and runner.proc.poll() is None:
                runner.proc.terminate()
        self._finish(runners)
        self._drop_connection()
        if self.exit_action == "uninstall":
            self._uninstall_self()
        return 0


# --- subcommands -----------------------------------------------------------------------


def cmd_run() -> int:
    return Supervisor().run()


def _service_call(fn: Any, **kwargs: Any) -> dict:
    try:
        return fn(**kwargs)
    except core.errors.WaError as exc:
        die(str(exc))


def cmd_install() -> int:
    _service_call(core.service.install_service, now=int(time.time()))
    print(f"installed {core.service.plist_path()}")
    return 0


def cmd_uninstall() -> int:
    _service_call(core.service.uninstall_service)
    print("uninstalled")
    return 0


def _age(ts: int | None, now: int) -> str:
    if not ts:
        return "-"
    delta = max(0, now - int(ts))
    if delta < 60:
        return f"{delta}s ago"
    if delta < 3600:
        return f"{delta // 60}m ago"
    return f"{delta // 3600}h ago"


def cmd_status() -> int:
    now = int(time.time())
    with closing(core.db.connect()) as conn:
        info = core.accounts.service_info(conn, now)
        accounts = core.accounts.list_accounts(conn)
    print(f"service: {'running' if info.get('running') else 'NOT running'}")
    print(f"  pid {info.get('pid') or '-'}  version {info.get('version') or '-'}  heartbeat {_age(info.get('heartbeat_at'), now)}")
    if not info.get("running"):
        print("  start it: Install service in the plugin UI, or `wa_channel.py install`")
    print(f"db: {core.db.db_path()}")
    if not accounts:
        print("accounts: none")
        return 0
    rows = [["ID", "KIND", "LABEL", "PHONE", "DESIRED", "STATE", "PORT", "HEARTBEAT", "ERROR"]]
    for acc in accounts:
        st = acc.get("status") or {}
        rows.append(
            [
                str(acc["id"]),
                str(acc.get("kind") or ""),
                str(acc.get("label") or ""),
                str(acc.get("phone") or "-"),
                str(acc.get("desired") or ""),
                str(st.get("state") or "-"),
                str(acc.get("port") or "-"),
                _age(st.get("heartbeat_at"), now),
                str(st.get("error") or ""),
            ]
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return 0


def cmd_install_skill() -> int:
    result = _service_call(core.service.install_skill, now=int(time.time()))
    print(f"installed skill to {result.get('path')}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("command", choices=["run", "install", "uninstall", "status", "install-skill"])
    command = parser.parse_args().command
    return {
        "run": cmd_run,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "install-skill": cmd_install_skill,
    }[command]()


if __name__ == "__main__":
    sys.exit(main())
