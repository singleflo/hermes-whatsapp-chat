"""Background-service + Hermes skill installer, runnable from the UI (no terminal steps).

The service is a LaunchAgent on macOS, a systemd user unit on Linux and a Startup-folder launcher with a
keep-alive supervisor on Windows. It installs itself at the first backend start (``ensure_service``)
unless the user uninstalled it. Everything is derived at runtime: the plugin directory from ``__file__``,
the interpreter and import path from the process that performs the install (the Hermes dashboard
backend, the only Python known to have fastapi/pydantic). Subprocess calls go through ``_run`` (the
test seam).
"""

from __future__ import annotations

import codecs
import getpass
import os
import plistlib
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from pathlib import Path
from typing import IO, Any

from . import db, errors

LABEL = "it.fl1.hermes-whatsapp-chat.channel"
UNIT_NAME = "hermes-whatsapp-chat-channel.service"
STARTUP_ENTRY_NAME = "hermes-whatsapp-chat-channel.vbs"
SKILL_NAME = "whatsapp-chat"
SKILL_TOKEN = "{{WA_CLI}}"
_MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"
_LINUX_PATH = "/usr/local/bin:/usr/bin:/bin"
_EXTRA_NODE_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")
_LINGER_DIR = Path("/var/lib/systemd/linger")  # test seam
_UNSUPPORTED = "The background service needs macOS, Linux (systemd) or Windows"

# Windows process creation flags (subprocess.CREATE_* exist only on Windows).
_NEW_PROCESS_GROUP = 0x00000200
_NO_WINDOW = 0x08000000
_BREAKAWAY_FROM_JOB = 0x01000000

# Version of the HTTP route surface. Hermes mounts plugin routes only at server startup, so after a plugin
# update the UI can talk to the old backend still in memory: the UI compares this with its required
# version. BUMP IT ON ANY ROUTE CHANGE (new/removed/changed route or response shape the UI relies on).
API_VERSION = 10
# The service heartbeat is written every second; older than this means the service is not alive.
SERVICE_STALE_SECONDS = 15
WINDOWS_STOP_SECONDS = 20.0


def _platform() -> str:  # test seam
    return sys.platform


# --- Paths -----------------------------------------------------------------------


def plugin_dir() -> Path:
    """The installed plugin root: wa_core -> dashboard -> plugin."""
    return Path(__file__).resolve().parents[2]


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def unit_path() -> Path:
    return Path.home() / ".config" / "systemd" / "user" / UNIT_NAME


def windows_startup_entry() -> Path:
    appdata = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / STARTUP_ENTRY_NAME


def skill_path() -> Path:
    return hermes_home() / "skills" / SKILL_NAME / "SKILL.md"


def _service_lock_path() -> Path:
    return db.data_dir() / "bin" / "service.lock"


def _optout_path() -> Path:
    return db.data_dir() / "service-disabled"


def stop_marker_path() -> Path:
    return db.data_dir() / "bin" / "channel.stop"


def channel_lock_path() -> Path:
    return db.data_dir() / "bin" / "channel.lock"


def launcher_paths() -> dict[str, Path]:
    bin_dir = db.data_dir() / "bin"
    if _platform() == "win32":
        return {"python": bin_dir / "hwc-python.cmd", "wa": bin_dir / "wa.cmd"}
    return {"python": bin_dir / "hwc-python", "wa": bin_dir / "wa"}


def _console_python() -> str:
    """Python that owns a console (``python.exe`` next to a windowless ``pythonw.exe``), so node children
    inherit one hidden console instead of each flashing a window."""
    if _platform() == "win32":
        console = Path(sys.executable).with_name("python.exe")
        if console.exists():
            return str(console)
    return sys.executable


def find_node() -> str | None:
    """Node binary: ``WA_NODE`` if usable, else PATH plus the usual install locations (a LaunchAgent/GUI
    process has a minimal PATH, so common dirs are searched explicitly)."""
    configured = os.environ.get("WA_NODE")
    if configured:
        found = shutil.which(configured)
        if found:
            return found
    tools = hermes_home() / "tools"  # Hermes-managed node: bin/ on POSIX, the root dir (node.exe) on Windows
    dirs = [
        os.environ.get("PATH", ""),
        str(Path.home() / ".local" / "bin"),
        str(hermes_home() / "node" / "bin"),
        str(Path.home() / ".hermes" / "node" / "bin"),
        *[str(p) for p in sorted(tools.glob("node-*/bin"), reverse=True)],
        *[str(p) for p in sorted(tools.glob("node-*"), reverse=True)],
        *_EXTRA_NODE_DIRS,
    ]
    return shutil.which("node", path=os.pathsep.join(d for d in dirs if d))


# --- Subprocess seams ------------------------------------------------------------------


def _run(argv: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=30,
        check=False,
        env=env,
        creationflags=_NO_WINDOW if _platform() == "win32" else 0,  # no console flash from a windowless backend
    )


def _spawn(argv: list[str], flags: int) -> subprocess.Popen:
    return subprocess.Popen(
        argv,
        creationflags=flags,
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _sleep(seconds: float) -> None:  # test seam
    time.sleep(seconds)


def _launchctl(argv: list[str]) -> subprocess.CompletedProcess | None:
    """Run launchctl; None when it cannot run at all (not macOS, timeout)."""
    try:
        return _run(["launchctl", *argv])
    except (OSError, subprocess.SubprocessError):
        return None


def _systemd_env() -> dict[str, str]:
    """Environment for ``systemctl --user``: a backend started outside a login session (SSH, a daemon)
    often lacks XDG_RUNTIME_DIR / DBUS_SESSION_BUS_ADDRESS, so repair them from /run/user/<uid>."""
    env = dict(os.environ)
    uid = os.getuid()

    def owned(path: str) -> bool:
        try:
            return os.stat(path).st_uid == uid
        except OSError:
            return False

    runtime = env.get("XDG_RUNTIME_DIR")
    if not runtime or not owned(runtime):
        fallback = f"/run/user/{uid}"
        if owned(fallback):
            env["XDG_RUNTIME_DIR"] = fallback
    runtime = env.get("XDG_RUNTIME_DIR")
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and runtime:
        bus = Path(runtime) / "bus"
        try:
            exists = bus.exists()
        except OSError:
            exists = False
        if exists:
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _systemctl(*args: str) -> subprocess.CompletedProcess | None:
    """Run ``systemctl --user``; None when it cannot run at all (no systemd, timeout)."""
    try:
        return _run(["systemctl", "--user", *args], env=_systemd_env())
    except (OSError, subprocess.SubprocessError):
        return None


# --- Locks -----------------------------------------------------------------------------


def _acquire_lock(path: Path) -> IO[bytes] | None:
    """Non-blocking exclusive lock on ``path``; None when another process holds it. The lock lives as
    long as the returned handle is open. The mechanism follows the real OS, not ``_platform()``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return None
    return handle


def _release_lock(handle: IO[bytes]) -> None:
    handle.close()  # closing the descriptor releases the lock on every OS


# --- Launchers -----------------------------------------------------------------------


def _write_if_changed(path: Path, content: str | bytes, mode: int | None = None) -> bool:
    """Write ``content`` unless the file already has exactly it; True when it was (re)written."""
    data = content.encode("utf-8") if isinstance(content, str) else content
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        unchanged = path.read_bytes() == data
    except OSError:
        unchanged = False
    if not unchanged:
        path.write_bytes(data)
    if mode is not None and (not path.is_file() or path.stat().st_mode & 0o777 != mode):
        path.chmod(mode)
        return True
    return not unchanged


def _import_path_entries() -> list[str]:
    seen: set[str] = set()
    entries: list[str] = []
    for entry in sys.path:
        if entry and entry not in seen and os.path.isdir(entry):
            seen.add(entry)
            entries.append(entry)
    return entries


def _cmd(value: str) -> str:
    """A value inside a .cmd file: ``%`` must be doubled."""
    return value.replace("%", "%%")


def _sync_launchers() -> tuple[dict[str, str], bool]:
    """Write the ``hwc-python`` launcher (this process' interpreter + import path) and the ``wa`` launcher
    into ``<data>/bin``; also reports whether either file changed."""
    paths = launcher_paths()
    entries = _import_path_entries()
    wa_script = plugin_dir() / "scripts" / "wa.py"
    if _platform() == "win32":
        python_text = "\r\n".join(
            [
                "@echo off",
                "rem Generated by hermes-whatsapp-chat: Hermes' Python with the import path of the plugin backend.",
                f'set "PYTHONPATH={_cmd(";".join(entries))}"',
                f'set "HERMES_HOME={_cmd(str(hermes_home()))}"',
                f'"{_cmd(_console_python())}" %*',
                "",
            ]
        )
        wa_text = "\r\n".join(
            [
                "@echo off",
                "rem Generated by hermes-whatsapp-chat: the WhatsApp chat CLI.",
                f'call "{_cmd(str(paths["python"]))}" "{_cmd(str(wa_script))}" %*',
                "",
            ]
        )
        mode = None
    else:
        python_text = (
            "#!/bin/sh\n"
            "# Generated by hermes-whatsapp-chat: Hermes' Python with the import path of the plugin backend.\n"
            f"export PYTHONPATH={shlex.quote(os.pathsep.join(entries))}\n"
            f"export HERMES_HOME={shlex.quote(str(hermes_home()))}\n"
            f"exec {shlex.quote(sys.executable)} \"$@\"\n"
        )
        wa_text = (
            "#!/bin/sh\n"
            "# Generated by hermes-whatsapp-chat: the WhatsApp chat CLI.\n"
            f"exec {shlex.quote(str(paths['python']))} {shlex.quote(str(wa_script))} \"$@\"\n"
        )
        mode = 0o755
    changed_python = _write_if_changed(paths["python"], python_text, mode)
    changed_wa = _write_if_changed(paths["wa"], wa_text, mode)
    return {"python": str(paths["python"]), "wa": str(paths["wa"])}, changed_python or changed_wa


def write_launchers(now: int | None = None) -> dict[str, str]:
    """Write the launchers (see ``_sync_launchers``)."""
    return _sync_launchers()[0]


def wa_command() -> str:
    """Shell-ready absolute path of the ``wa`` launcher; launchers are written lazily when missing."""
    wa = launcher_paths()["wa"]
    if not wa.is_file():
        try:
            write_launchers()
        except OSError:
            pass
    if _platform() == "win32":
        return subprocess.list2cmdline([str(wa)])
    return shlex.quote(str(wa))


# --- Service files --------------------------------------------------------------------


def _supported() -> bool:
    platform = _platform()
    return platform == "darwin" or platform.startswith("linux") or platform == "win32"


def service_installed() -> bool:
    platform = _platform()
    if platform == "darwin":
        return plist_path().is_file()
    if platform.startswith("linux"):
        return unit_path().is_file()
    if platform == "win32":
        return windows_startup_entry().is_file()
    return False


def auto_install_enabled() -> bool:
    """False once the user uninstalled the service from the UI or CLI (until Install service is used)."""
    return not _optout_path().exists()


def _sd(value: str) -> str:
    """A value in a systemd unit file: quoted, ``%`` specifiers escaped."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def _vbs(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _service_env(node: str) -> dict[str, str]:
    env = {"WA_NODE": node, "HERMES_HOME": str(hermes_home())}
    if os.environ.get("WA_ARCHIVE_DB"):
        env["WA_ARCHIVE_DB"] = os.environ["WA_ARCHIVE_DB"]
    return env


def _write_plist(launchers: dict[str, str], node: str, logs: Path) -> tuple[Path, bool]:
    plugin = plugin_dir()
    env = {**_service_env(node), "PATH": f"{Path(node).parent}:{_MINIMAL_PATH}"}
    plist = {
        "Label": LABEL,
        "ProgramArguments": [launchers["python"], str(plugin / "sidecar" / "wa_channel.py"), "run"],
        "EnvironmentVariables": env,
        "WorkingDirectory": str(plugin),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": str(logs / "channel.log"),
        "StandardErrorPath": str(logs / "channel.log"),
    }
    target = plist_path()
    return target, _write_if_changed(target, plistlib.dumps(plist))


def _write_unit(launchers: dict[str, str], node: str, logs: Path) -> tuple[Path, bool]:
    plugin = plugin_dir()
    env = {**_service_env(node), "PATH": f"{Path(node).parent}:{_LINUX_PATH}"}
    lines = [
        "[Unit]",
        "Description=Hermes WhatsApp Chat channel",
        "",
        "[Service]",
        f"ExecStart={_sd(launchers['python'])} {_sd(str(plugin / 'sidecar' / 'wa_channel.py'))} run",
        # WorkingDirectory= takes the raw path (quotes make systemd reject it as "not absolute").
        f"WorkingDirectory={str(plugin).replace('%', '%%')}",
        *[f"Environment={_sd(f'{name}={value}')}" for name, value in env.items()],
        "Restart=always",
        "RestartSec=10",
        f"StandardOutput=append:{logs / 'channel.log'}",
        f"StandardError=append:{logs / 'channel.log'}",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ]
    target = unit_path()
    return target, _write_if_changed(target, "\n".join(lines))


def _write_startup_entry(launchers: dict[str, str], node: str, logs: Path) -> tuple[Path, bool]:
    sidecar = plugin_dir() / "sidecar"
    command = subprocess.list2cmdline(
        [_console_python(), str(sidecar / "supervise.py"), str(sidecar / "wa_channel.py"), str(db.data_dir())]
    )
    env = _service_env(node)
    lines = [
        "' Generated by hermes-whatsapp-chat: starts the WhatsApp channel hidden at sign-in.",
        "Option Explicit",
        "Dim sh, env",
        'Set sh = CreateObject("WScript.Shell")',
        'Set env = sh.Environment("Process")',
        f'env("PYTHONPATH") = {_vbs(";".join(_import_path_entries()))}',
        f'env("HERMES_HOME") = {_vbs(env["HERMES_HOME"])}',
        f'env("WA_NODE") = {_vbs(node)}',
        f'env("PATH") = {_vbs(str(Path(node).parent) + ";")} & env("PATH")',
        *([f'env("WA_ARCHIVE_DB") = {_vbs(env["WA_ARCHIVE_DB"])}'] if "WA_ARCHIVE_DB" in env else []),
        f"sh.Run {_vbs(command)}, 0, False",
        "",
    ]
    data = codecs.BOM_UTF16_LE + "\r\n".join(lines).encode("utf-16-le")
    target = windows_startup_entry()
    return target, _write_if_changed(target, data)


def _sync_service_files(node: str) -> tuple[dict[str, str], Path, bool]:
    """Write launchers + the platform's service file; returns (launcher paths, service file path, whether
    any of them changed)."""
    if not _supported():
        raise errors.Unavailable(_UNSUPPORTED)
    launchers, launchers_changed = _sync_launchers()
    logs = db.data_dir() / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    platform = _platform()
    if platform == "darwin":
        target, changed = _write_plist(launchers, node, logs)
    elif platform.startswith("linux"):
        target, changed = _write_unit(launchers, node, logs)
    else:
        target, changed = _write_startup_entry(launchers, node, logs)
    return launchers, target, launchers_changed or changed


# --- Start / stop ------------------------------------------------------------------------

# launchd unloads asynchronously: a bootstrap issued right after bootout fails with
# "Bootstrap failed: 5: Input/output error" until the old job is gone, so retry for a few seconds.
BOOTSTRAP_ATTEMPTS = 20
BOOTSTRAP_RETRY_SECONDS = 0.5


def linger_enabled() -> bool | None:
    """Whether the user's systemd instance survives logout (Linux only; None elsewhere or unknown)."""
    if not _platform().startswith("linux"):
        return None
    try:
        return (_LINGER_DIR / getpass.getuser()).exists()
    except (OSError, KeyError):
        return None


def _reload_launchd(target: Path) -> None:
    """Always (re)start the service: bootout, then bootstrap (retried while launchd finishes unloading the
    old job), then ``kickstart -k`` as a last resort. Raises ``Unavailable`` when launchctl cannot run or
    every attempt fails."""
    domain = f"gui/{os.getuid()}"
    _launchctl(["bootout", f"{domain}/{LABEL}"])  # not loaded yet is fine
    result = None
    for attempt in range(BOOTSTRAP_ATTEMPTS):
        result = _launchctl(["bootstrap", domain, str(target)])
        if result is None:
            raise errors.Unavailable("launchctl is not available: the background service needs macOS")
        if result.returncode == 0:
            return
        if attempt + 1 < BOOTSTRAP_ATTEMPTS:
            _sleep(BOOTSTRAP_RETRY_SECONDS)
    kick = _launchctl(["kickstart", "-k", f"{domain}/{LABEL}"])
    if kick is not None and kick.returncode == 0:
        return
    assert result is not None
    detail = (result.stderr or result.stdout or "").strip() or f"exit code {result.returncode}"
    raise errors.Unavailable(f"launchctl bootstrap failed: {detail}")


def _reload_systemd() -> None:
    """Enable lingering when it is off, then daemon-reload, enable and restart the user unit."""
    if linger_enabled() is False:
        try:
            _run(["loginctl", "enable-linger", getpass.getuser()])  # best effort: needs privileges
        except (OSError, subprocess.SubprocessError, KeyError):
            pass
    for args in (["daemon-reload"], ["enable", UNIT_NAME], ["restart", UNIT_NAME]):
        result = _systemctl(*args)
        if result is None:
            raise errors.Unavailable("systemctl is not available: the background service needs systemd on Linux")
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() or f"exit code {result.returncode}"
            raise errors.Unavailable(f"systemctl {args[0]} failed: {detail}")


def _windows_stop(timeout: float = WINDOWS_STOP_SECONDS) -> bool:
    """Ask the running channel to exit through the stop marker and wait until its supervisor released the
    channel lock. The marker is always removed again."""
    marker = stop_marker_path()
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("stop\n", encoding="utf-8")
    attempts = int(timeout / 0.5) + 1
    try:
        for attempt in range(attempts):
            handle = _acquire_lock(channel_lock_path())
            if handle is not None:
                _release_lock(handle)
                return True
            if attempt + 1 < attempts:
                _sleep(0.5)
        return False
    finally:
        marker.unlink(missing_ok=True)


def _windows_start(entry: Path) -> None:
    argv = ["wscript.exe", "//B", "//Nologo", str(entry)]
    try:
        _spawn(argv, _NEW_PROCESS_GROUP | _NO_WINDOW | _BREAKAWAY_FROM_JOB)
    except OSError:  # a job object that forbids breakaway
        try:
            _spawn(argv, _NEW_PROCESS_GROUP | _NO_WINDOW)
        except OSError as exc:
            raise errors.Unavailable(f"Could not start the WhatsApp service: {exc}")


def _reload_service(target: Path) -> None:
    """Always (re)start the service on this platform. Raises ``Unavailable`` when it cannot."""
    platform = _platform()
    if platform == "darwin":
        _reload_launchd(target)
    elif platform.startswith("linux"):
        _reload_systemd()
    elif platform == "win32":
        if not _windows_stop():
            raise errors.Unavailable(f"The running WhatsApp service did not stop within {WINDOWS_STOP_SECONDS:.0f} s")
        _windows_start(target)
    else:
        raise errors.Unavailable(_UNSUPPORTED)


def install_service(*, now: int) -> dict[str, Any]:
    if not _supported():
        raise errors.Unavailable(_UNSUPPORTED)
    node = find_node()
    if not node:
        raise errors.Unavailable("Node.js not found: install Node 20+ (or set WA_NODE), then install the service again")
    _optout_path().unlink(missing_ok=True)  # an explicit install turns automatic install back on
    launchers, target, _ = _sync_service_files(node)
    _reload_service(target)  # always restarts, even when nothing changed
    return {"path": str(target), "node": node, **launchers}


def _heartbeat_at() -> int | None:
    with closing(db.connect()) as conn:
        row = conn.execute("SELECT heartbeat_at FROM service_status WHERE id = 1").fetchone()
    return row["heartbeat_at"] if row else None


def ensure_service(*, now: int) -> str:
    """Install or refresh the service; called at every backend start.

    Not installed: installs it unless the user uninstalled it (``disabled``) or the platform has no
    service support (``unsupported``) → ``installed``. Installed: rewrites the launchers + service file
    from the running code and restarts the service when any file changed or its heartbeat is stale →
    ``ok`` or ``restarted``. ``busy`` when another process is running this right now, ``error`` on any
    failure. A file lock serialises concurrent Hermes processes."""
    try:
        lock = _acquire_lock(_service_lock_path())
    except OSError:
        return "error"
    if lock is None:
        return "busy"
    try:
        installed = service_installed()
        if not installed:
            if not auto_install_enabled():
                return "disabled"
            if not _supported():
                return "unsupported"
        node = find_node()
        if not node:
            return "error"
        _, target, changed = _sync_service_files(node)
        if not installed:
            _reload_service(target)
            return "installed"
        heartbeat = _heartbeat_at()
        stale = heartbeat is None or now - heartbeat > SERVICE_STALE_SECONDS
        if not (changed or stale):
            return "ok"
        _reload_service(target)
        return "restarted"
    except (errors.WaError, OSError, sqlite3.Error):
        return "error"
    finally:
        _release_lock(lock)


def uninstall_service() -> dict[str, Any]:
    platform = _platform()
    if platform == "darwin":
        target = plist_path()
        _launchctl(["bootout", f"gui/{os.getuid()}/{LABEL}"])  # failure just means it was not loaded
    elif platform.startswith("linux"):
        target = unit_path()
        _systemctl("disable", "--now", UNIT_NAME)  # failure just means it was not enabled
    elif platform == "win32":
        target = windows_startup_entry()
        _windows_stop()  # result ignored: the entry is removed either way
    else:
        raise errors.Unavailable(_UNSUPPORTED)
    existed = target.exists()
    target.unlink(missing_ok=True)
    if platform.startswith("linux"):
        _systemctl("daemon-reload")
    optout = _optout_path()
    optout.parent.mkdir(parents=True, exist_ok=True)
    optout.write_text("uninstalled from the UI or CLI\n", encoding="utf-8")
    return {"path": str(target), "removed": existed}


# --- Hermes skill ------------------------------------------------------------------


def skill_installed() -> bool:
    return skill_path().is_file()


def install_skill(*, now: int) -> dict[str, str]:
    template = plugin_dir() / "skill" / SKILL_NAME / "SKILL.md"
    try:
        text = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise errors.Unavailable(f"skill template missing: {template} ({exc})")
    write_launchers(now)
    target = skill_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text.replace(SKILL_TOKEN, wa_command()), encoding="utf-8")
    return {"path": str(target)}


def uninstall_skill() -> dict[str, bool]:
    target = skill_path()
    target.unlink(missing_ok=True)
    try:
        target.parent.rmdir()  # only if we left it empty
    except OSError:
        pass
    return {"ok": True}
