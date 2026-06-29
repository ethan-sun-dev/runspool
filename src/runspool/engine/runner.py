"""Single-step executor: runs one step in a worker thread and applies the
resulting state transition."""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import Any

from runspool.clock import utcnow_text
from runspool.engine.registry import StepRegistry
from runspool.engine.step import StepContext, StepDeferred
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository
from runspool.persistence.state_machine import StateMachine
from runspool.persistence.step_run_log import StepRunLog

# Minimum interval (seconds) between heartbeat/progress writes. High-frequency
# progress callbacks persist at most once per interval to avoid hammering SQLite.
_HEARTBEAT_MIN_INTERVAL = 1.0


def _default_notifier(message: str) -> None:
    """Print exceptions / failures / terminations to stderr so a foreground
    daemon console shows them directly."""
    print(message, file=sys.stderr, flush=True)


class TaskRunner:
    def __init__(
        self,
        repo: TaskRepository,
        log: EventLog,
        step_runs: StepRunLog,
        registry: StepRegistry,
        config: Any,
        *,
        monotonic: Any = time.monotonic,
        notifier: Callable[[str], None] = _default_notifier,
    ) -> None:
        self.repo = repo
        self.log = log
        self.step_runs = step_runs
        self.registry = registry
        self.config = config
        self._monotonic = monotonic
        self._notifier = notifier

    def _notify(self, task: dict[str, Any], reason: str) -> None:
        # Prefer the task name; fall back to the raw input so the console can tell
        # tasks apart at a glance.
        label = task.get("name") or task["input"]
        self._notifier(f"[{utcnow_text()}] task #{task['id']} ({label}) {reason}")

    def execute(self, task_id: int) -> None:
        task = self.repo.get_task(task_id)
        if task is None:
            return
        sm = StateMachine(self.repo, self.log, workflow=self.config.workflow(task["workflow"]))
        step = self.registry.get(task["step"])

        # Heartbeat / progress: throttled centrally. No progress string refreshes
        # the heartbeat only; a string also persists ``progress``.
        hb_state: dict[str, float | None] = {"last": None}

        def _heartbeat(progress: str | None = None) -> None:
            now = self._monotonic()
            last = hb_state["last"]
            if last is not None and now - last < _HEARTBEAT_MIN_INTERVAL:
                return
            hb_state["last"] = now
            fields: dict[str, Any] = {"heartbeat_at": utcnow_text()}
            if progress is not None:
                fields["progress"] = progress
            self.repo.update_fields(task_id, fields)

        ctx = StepContext(
            task=task,
            config=self.config,
            should_stop=lambda: self._stop_requested(task_id),
            heartbeat=_heartbeat,
        )
        # Clear leftover progress from the previous step so we never show a stale
        # 100% before the next step reports anything.
        self.repo.update_fields(task_id, {"progress": None})
        run_id = self.step_runs.start(task_id, task["step"])
        t0 = time.monotonic()
        # Both the step run and persisting its updates are covered by failure
        # handling: any exception marks the step failed and routes through
        # sm.fail, so a step_run never hangs in "running" and a task never gets
        # stuck in "running".
        try:
            result = step.run(ctx)
            if result.updates:
                self.repo.update_fields(task_id, result.updates)
        except StepDeferred:
            dur = int((time.monotonic() - t0) * 1000)
            self.step_runs.finish(run_id, status="deferred", duration_ms=dur)
            sm.defer(task_id)
            return
        except Exception as exc:  # noqa: BLE001 - any step failure becomes task failure
            dur = int((time.monotonic() - t0) * 1000)
            message = f"{type(exc).__name__}: {exc}"
            self.step_runs.finish(run_id, status="failed", duration_ms=dur, error=message)
            sm.fail(
                task_id,
                message,
                retry_delay_seconds=self.config.scheduler.retry_delay_seconds,
            )
            # fail() may go to failed (will retry) or manual_required; report
            # based on the freshly persisted status.
            fresh = self.repo.get_task(task_id)
            verb = "failed" if fresh and fresh["task_status"] == "failed" else "needs attention"
            self._notify(fresh or task, f"step {task['step']} raised ({verb}): {message}")
            return
        dur = int((time.monotonic() - t0) * 1000)
        self.step_runs.finish(run_id, status="ok", duration_ms=dur)
        fresh = self.repo.get_task(task_id)
        if fresh["terminate_requested"]:
            sm.apply_terminate(task_id)
            self._notify(fresh, f"terminated after step {task['step']}")
        elif fresh["pause_requested"]:
            # The step finished; pause at the boundary by advancing first so the
            # completed step is not re-run on resume.
            sm.pause_after_successful_step(task_id)
            done = self.repo.get_task(task_id)
            if done and done["task_status"] == "completed":
                self._notify(done, f"step {task['step']} done, workflow complete")
            else:
                nxt = done["step"] if done else "?"
                self._notify(done or fresh, f"step {task['step']} done, paused before {nxt}")
        else:
            sm.complete_step(task_id)
            # Report success too, distinguishing "all done" from "next step".
            done = self.repo.get_task(task_id)
            tail = f": {result.message}" if result.message else ""
            if done and done["task_status"] == "completed":
                self._notify(done, f"step {task['step']} done, workflow complete{tail}")
            else:
                nxt = done["step"] if done else "?"
                self._notify(done or fresh, f"step {task['step']} done, advancing to {nxt}{tail}")

    def _stop_requested(self, task_id: int) -> bool:
        # should_stop signals termination only. Pause is applied at step
        # boundaries (see pause_after_successful_step), so a running step is
        # always allowed to finish rather than being interrupted mid-work.
        task = self.repo.get_task(task_id)
        return bool(task and task["terminate_requested"])
