"""Daemon: a resident loop driving the Coordinator, with PID management,
startup recovery, and graceful shutdown."""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
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


@dataclass(frozen=True)
class DaemonTick:
    """One daemon round, after its scheduling work: what ``daemon/tick`` carries.

    ``round`` counts from 1 for each daemon run; ``now`` is ``time.time()``.
    """

    round: int
    now: float


class Daemon:
    def __init__(
        self,
        coordinator: Coordinator,
        config: Any,
        *,
        on_tick: Callable[[DaemonTick], None] | None = None,
    ) -> None:
        self.coordinator = coordinator
        self.config = config
        # Called once per round after scheduling (maintenance hooks). It runs on the
        # daemon's own thread, so it must return quickly.
        self.on_tick = on_tick
        self._stop = threading.Event()
        self._round = 0

    def recover(self) -> None:
        # Startup recovery: recover_interrupted only changes task_status / lock
        # fields and does not depend on specific steps, so any defined workflow
        # builds a usable StateMachine.
        any_workflow = next(iter(self.config.workflows))
        sm = StateMachine(
            self.coordinator.repo,
            self.coordinator.log,
            workflow=self.config.workflow(any_workflow),
            step_runs=self.coordinator.runner.step_runs,
        )
        sm.recover_interrupted()

    def run_once(self) -> None:
        # Non-blocking: tick submits runnable steps to the background pool and
        # returns immediately. Long steps run across many ticks in the pool;
        # RUNNING tasks are not re-claimed, so the daemon keeps processing others.
        timeout = self.config.worker_pool.heartbeat_timeout_seconds
        self.coordinator.reclaim_stale(timeout)
        self.coordinator.tick()

    def _tick(self) -> None:
        if self.on_tick is None:
            return
        self._round += 1
        # Maintenance must never stop scheduling: log and carry on.
        try:
            self.on_tick(DaemonTick(round=self._round, now=time.time()))
        except Exception:  # noqa: BLE001 - a broken hook must not kill the daemon
            logger.exception("daemon maintenance hook failed; continuing")

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
                self._tick()
                self._stop.wait(timeout=interval)
        finally:
            self.coordinator.pool.shutdown(wait=True)
