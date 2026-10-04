"""Single-step executor: runs one step in a worker thread and applies the
resulting state transition."""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from runspool.clock import utcnow_text
from runspool.engine.gate import Allow, Ask, Decision, Defer, Deny, GateRequest
from runspool.engine.registry import StepRegistry
from runspool.engine.step import StepContext, StepDeferred
from runspool.models import TaskStatus
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository
from runspool.persistence.state_machine import (
    Deferred,
    Denied,
    Failed,
    NeedsApproval,
    StateMachine,
    Succeeded,
)
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
        heartbeat_interval: float | None = None,
        gate: Callable[[GateRequest], Decision] | None = None,
        on_approval_asked: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self.repo = repo
        self.log = log
        self.step_runs = step_runs
        self.registry = registry
        self.config = config
        self._monotonic = monotonic
        self._notifier = notifier
        # Without a gate there is nobody to ask: steps with side effects are refused
        # (fail closed) rather than run unapproved.
        self._gate = gate
        self._on_approval_asked = on_approval_asked
        # Refresh the heartbeat on a timer while a step runs, independent of
        # whether the step cooperatively calls ctx.heartbeat(). Default to a
        # third of the reclaim timeout so a healthy long step is never reclaimed.
        if heartbeat_interval is None:
            heartbeat_interval = config.worker_pool.heartbeat_timeout_seconds / 3.0
        self._heartbeat_interval = heartbeat_interval

    def _notify(self, task: dict[str, Any], reason: str) -> None:
        # Prefer the task name; fall back to the raw input so the console can tell
        # tasks apart at a glance.
        label = task.get("name") or task["input"]
        self._notifier(f"[{utcnow_text()}] task #{task['id']} ({label}) {reason}")

    def execute(self, task_id: int, *, claim_token: str | None = None) -> None:
        task = self.repo.get_task(task_id)
        if task is None:
            return
        # Stale-request guard: while a job waited in a backlogged pool, its task may
        # have been reclaimed, requeued, advanced or finished. A job carrying a claim
        # token runs only if that claim is still current, so it never runs whatever
        # step the task is on now. (No token: direct calls from tests and scripts.)
        if claim_token is not None and (
            task["task_status"] not in (TaskStatus.RUNNING, TaskStatus.PAUSE_PENDING)
            or task["claim_token"] != claim_token
        ):
            self._notify(
                task, f"skipped a stale run request (now {task['task_status']} on {task['step']})"
            )
            return
        sm = StateMachine(
            self.repo,
            self.log,
            workflow=self.config.workflow(task["workflow"]),
            step_runs=self.step_runs,
        )
        if task["terminate_requested"] or task["pause_requested"]:
            # Requested while the job waited in the pool: honour it before starting
            # the step (terminate, or pause in place) instead of running it first.
            after = sm.finish_step(task_id, Deferred("not started"), token=claim_token)
            if after is not None:
                self._notify(after, f"{after['task_status']} before step {task['step']} started")
            return
        step = self.registry.get(task["step"])
        attempt = self.step_runs.count_runs(task_id, task["step"]) + 1
        if not self._admit(sm, task, step, attempt, claim_token):
            return

        # Heartbeat / progress: throttled centrally. No progress string refreshes
        # the heartbeat only; a string also persists ``progress``.
        hb_state: dict[str, float | None] = {"last": None}

        def _heartbeat(progress: str | None = None) -> None:
            now = self._monotonic()
            last = hb_state["last"]
            if last is not None and now - last < _HEARTBEAT_MIN_INTERVAL:
                return
            hb_state["last"] = now
            if progress is None:
                self.repo.heartbeat(task_id, at=utcnow_text(), token=claim_token)
            else:
                self.repo.heartbeat(task_id, at=utcnow_text(), progress=progress, token=claim_token)

        ctx = StepContext(
            task=task,
            config=self.config,
            should_stop=lambda: self._stop_requested(task_id),
            heartbeat=_heartbeat,
            attempt=attempt,
        )
        # Clear leftover progress from the previous step so we never show a stale
        # 100% before the next step reports anything.
        self.repo.update_fields(task_id, {"progress": None}, token=claim_token)
        run_id = self.step_runs.start(task_id, task["step"])
        t0 = time.monotonic()
        # Both the step run and persisting its updates are covered by failure
        # handling: any exception marks the step failed, so a step_run never hangs
        # in "running" and a task never gets stuck in "running".
        try:
            # The background heartbeat is stopped (in the inner finally) before any
            # state transition runs, so it can never refresh a task after its
            # transition has released it.
            stop_beat = threading.Event()
            beat = threading.Thread(
                target=self._beat_loop, args=(task_id, stop_beat, claim_token), daemon=True
            )
            beat.start()
            try:
                result = step.run(ctx)
            finally:
                stop_beat.set()
                beat.join()
            if result.updates:
                self.repo.update_fields(task_id, result.updates, token=claim_token)
        except StepDeferred as deferred:
            self.step_runs.finish(
                run_id, status="deferred", duration_ms=_ms_since(t0), note=deferred.reason
            )
            after = sm.finish_step(
                task_id, Deferred(deferred.reason, deferred.delay_seconds), token=claim_token
            )
            if after and after["task_status"] == TaskStatus.TERMINATED:
                self._notify(after, f"terminated after step {task['step']}")
            return
        except Exception as exc:  # noqa: BLE001 - any step failure becomes task failure
            message = f"{type(exc).__name__}: {exc}"
            self.step_runs.finish(run_id, status="failed", duration_ms=_ms_since(t0), error=message)
            after = sm.finish_step(
                task_id,
                Failed(message, self.config.scheduler.retry_delay_seconds),
                token=claim_token,
            )
            status = after["task_status"] if after else None
            verb = {
                TaskStatus.FAILED: "failed",
                TaskStatus.PAUSED: "failed, paused",
                TaskStatus.TERMINATED: "failed, terminated",
            }.get(status, "needs attention")
            self._notify(after or task, f"step {task['step']} raised ({verb}): {message}")
            return
        self.step_runs.finish(
            run_id,
            status="degraded" if result.degraded else "ok",
            duration_ms=_ms_since(t0),
            note=result.message or None,
        )
        after = sm.finish_step(task_id, Succeeded(result.degraded), token=claim_token)
        self._report_success(task, after, result)

    def _admit(
        self, sm: StateMachine, task: dict[str, Any], step: Any, attempt: int, token: str | None
    ) -> bool:
        """Ask the gate whether the step may run now; resolve the task if not.

        A step refused, deferred or held for approval does not run and records no
        step run, so the attempt it is approved for is the one that runs next.
        """
        request = GateRequest(task, step, attempt)
        try:
            if self._gate is not None:
                decision = self._gate(request)
            elif getattr(step, "side_effect", False) and not request.granted:
                decision = Deny("the step has side effects and no approval gate is configured")
            else:
                decision = Allow()
        except Exception as exc:  # noqa: BLE001 - a broken gate fails closed
            decision = Deny(f"pre-execute gate failed: {type(exc).__name__}: {exc}")
        if isinstance(decision, Allow):
            return True
        if isinstance(decision, Defer):
            outcome: Any = Deferred(decision.reason, decision.delay_seconds)
        elif isinstance(decision, Ask):
            outcome = NeedsApproval(decision.reason, attempt)
        else:
            reason = decision.reason if isinstance(decision, Deny) else repr(decision)
            outcome = Denied(reason)
        after = sm.finish_step(task["id"], outcome, token=token)
        if after is None:
            return False
        status = after["task_status"]
        if status == TaskStatus.AWAITING_APPROVAL:
            self._notify(after, f"step {task['step']} awaits approval: {decision.reason}")
            if self._on_approval_asked is not None:
                self._on_approval_asked(after)
        elif status == TaskStatus.MANUAL_REQUIRED:
            self._notify(after, f"step {task['step']} not run: {after['last_error']}")
        return False

    def _report_success(
        self, task: dict[str, Any], after: dict[str, Any] | None, result: Any
    ) -> None:
        step = task["step"]
        tail = f": {result.message}" if result.message else ""
        status = after["task_status"] if after else None
        if status == TaskStatus.TERMINATED:
            self._notify(after, f"terminated after step {step}")
        elif status == TaskStatus.COMPLETED:
            self._notify(after, f"step {step} done, workflow complete{tail}")
        elif status == TaskStatus.PARTIALLY_COMPLETED:
            self._notify(after, f"step {step} done, workflow partially complete{tail}")
        elif status == TaskStatus.PAUSED:
            self._notify(after, f"step {step} done, paused before {after['step']}")
        elif status == TaskStatus.QUEUED:
            self._notify(after, f"step {step} done, advancing to {after['step']}{tail}")

    def _beat_loop(self, task_id: int, stop: threading.Event, token: str | None) -> None:
        # Periodically refresh heartbeat_at until signalled to stop. Best-effort:
        # a transient DB error must not crash the worker thread.
        while not stop.wait(self._heartbeat_interval):
            try:
                self.repo.heartbeat(task_id, at=utcnow_text(), token=token)
            except Exception:  # noqa: BLE001 - heartbeat is best-effort
                pass

    def _stop_requested(self, task_id: int) -> bool:
        # should_stop signals termination only. Pause is applied at step
        # boundaries (see pause_after_successful_step), so a running step is
        # always allowed to finish rather than being interrupted mid-work.
        task = self.repo.get_task(task_id)
        return bool(task and task["terminate_requested"])


def _ms_since(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)
