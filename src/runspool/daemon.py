"""Daemon: a resident loop driving the Coordinator, with PID management,
startup recovery, and graceful shutdown."""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from runspool.engine.coordinator import Coordinator
from runspool.persistence.state_machine import StateMachine

logger = logging.getLogger(__name__)


def write_pid(pid_file: Path, pid: int) -> None:
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(pid), encoding="utf-8")


def read_pid(pid_file: Path) -> int | None:
    try:
        return int(Path(pid_file).read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        return None


class Daemon:
    def __init__(self, coordinator: Coordinator, config: Any) -> None:
        self.coordinator = coordinator
        self.config = config
        self._stop = threading.Event()

    def recover(self) -> None:
        # Startup recovery: recover_interrupted only changes task_status / lock
        # fields and does not depend on specific steps, so any defined workflow
        # builds a usable StateMachine.
        any_workflow = next(iter(self.config.workflows))
        sm = StateMachine(
            self.coordinator.repo,
            self.coordinator.log,
            workflow=self.config.workflow(any_workflow),
        )
        sm.recover_interrupted()

    def run_once(self) -> None:
        # Non-blocking: tick submits runnable steps to the background pool and
        # returns immediately. Long steps run across many ticks in the pool;
        # RUNNING tasks are not re-claimed, so the daemon keeps processing others.
        timeout = self.config.worker_pool.heartbeat_timeout_seconds
        self.coordinator.reclaim_stale(timeout)
        self.coordinator.tick()

    def request_stop(self) -> None:
        self._stop.set()

    def run(self, poll_interval_seconds: float | None = None) -> None:
        interval = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else self.config.scheduler.poll_interval_seconds
        )
        self.recover()
        try:
            while not self._stop.is_set():
                # A single bad round must not kill the daemon: log and continue.
                try:
                    self.run_once()
                except Exception:  # noqa: BLE001 - tolerate transient errors
                    logger.exception("daemon tick failed; continuing")
                self._stop.wait(timeout=interval)
        finally:
            self.coordinator.pool.shutdown(wait=True)
