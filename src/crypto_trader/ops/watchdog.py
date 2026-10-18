"""In-process watchdog: if the main loop stops ticking, exit so Docker's restart policy brings the service back."""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable

log = logging.getLogger(__name__)


class Watchdog:
    def __init__(self, name: str, timeout_s: float = 300, on_stall: Callable[[str], None] | None = None,
                 exit_fn: Callable[[int], None] = os._exit):
        self.name, self.timeout_s = name, timeout_s
        self.on_stall, self.exit_fn = on_stall, exit_fn
        self.last = time.monotonic()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> None:
        self.last = time.monotonic()

    def stalled(self) -> bool:
        return time.monotonic() - self.last > self.timeout_s

    def check_once(self) -> bool:
        if self.stalled():
            msg = f"{self.name}: main loop stalled > {self.timeout_s:.0f}s, exiting for restart"
            log.critical(msg)
            if self.on_stall:
                try:
                    self.on_stall(msg)
                except Exception:  # noqa: BLE001
                    pass
            self.exit_fn(3)
            return True
        return False

    def start(self, interval_s: float = 15) -> Watchdog:
        def loop():
            while not self._stop.wait(interval_s):
                if self.check_once():
                    return

        self._thread = threading.Thread(target=loop, name=f"watchdog-{self.name}", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
