"""Keep-alive supervisor for the WhatsApp channel on Windows (launchd and systemd do this elsewhere).

Started hidden at sign-in by the Startup-folder launcher written by ``wa_core/service.py``:

    python.exe supervise.py <wa_channel.py> <data dir>

Holds ``<data>/bin/channel.lock`` while it runs, appends the channel's output to
``<data>/logs/channel.log`` and restarts ``wa_channel.py run`` 10 s after it exits, unless it exits with
``STOP_EXIT`` (service uninstalled / stop requested) or ``<data>/bin/channel.stop`` exists. Standard
library only.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import IO, Callable

STOP_EXIT = 3
RESTART_DELAY_SECONDS = 10


def acquire(path: Path) -> IO[bytes] | None:
    """Exclusive non-blocking lock on byte 0 of ``path`` (Windows only); None when another supervisor
    holds it. The lock lives as long as the returned handle is open."""
    if sys.platform != "win32":
        raise RuntimeError("supervise.py is the Windows keep-alive; launchd/systemd supervise elsewhere")
    import msvcrt

    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+b")
    try:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        return None
    return handle


def run_loop(spawn: Callable[[], int], stop_requested: Callable[[], bool], sleep: Callable[[float], None]) -> None:
    """Run ``spawn`` until it returns ``STOP_EXIT`` or a stop is requested; wait between runs."""
    while not stop_requested():
        if spawn() == STOP_EXIT:
            return
        for _ in range(RESTART_DELAY_SECONDS):
            if stop_requested():
                return
            sleep(1)


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: supervise.py <wa_channel.py> <data dir>", file=sys.stderr)
        return 2
    channel, data = argv[1], Path(argv[2])
    lock = acquire(data / "bin" / "channel.lock")
    if lock is None:
        return 0  # another supervisor is already running
    logs = data / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    stop_marker = data / "bin" / "channel.stop"

    with open(logs / "channel.log", "ab") as log:

        def spawn() -> int:
            return subprocess.call(
                [sys.executable, channel, "run"],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )

        run_loop(spawn, stop_marker.exists, time.sleep)
    lock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
