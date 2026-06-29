"""Bounded background worker pool wrapping a ThreadPoolExecutor."""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor


class WorkerPool:
    def __init__(self, size: int) -> None:
        self.size = size
        self._executor = ThreadPoolExecutor(max_workers=size, thread_name_prefix="runspool-worker")
        self._pending: list[Future[None]] = []
        self._lock = threading.Lock()

    def submit(self, fn: Callable[[], None]) -> None:
        future = self._executor.submit(fn)
        with self._lock:
            # Sweep completed futures so a long-running daemon does not grow
            # _pending without bound.
            self._pending = [f for f in self._pending if not f.done()]
            self._pending.append(future)

    def drain(self) -> None:
        """Wait for all currently submitted tasks to finish, then clear the list."""
        with self._lock:
            pending = self._pending[:]
            self._pending.clear()
        for f in pending:
            f.result()  # re-raises step exceptions; caller decides what to do

    def shutdown(self, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait)
