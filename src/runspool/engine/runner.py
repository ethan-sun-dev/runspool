"""Single-step executor: runs one step in a worker thread and applies the
resulting state transition."""

from __future__ import annotations

import json
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any

from runspool.clock import utcnow_text
from runspool.engine.artifacts import contract_of, prepare_artifacts
from runspool.engine.children import SpawnPlan, plan_children
from runspool.engine.external import Submission, SubmissionRejected, is_external
from runspool.engine.gate import Allow, Ask, Decision, Defer, Deny, GateRequest
from runspool.engine.registry import StepRegistry
from runspool.engine.step import StepContext, StepDeferred
from runspool.models import TaskStatus
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository
from runspool.persistence.state_machine import (
    AwaitsExternal,
    Deferred,
    Denied,
    Failed,
    NeedsApproval,
    Rejected,
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
        artifacts: Any = None,
        items: Any = None,
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
        # Where StepResult.artifacts are recorded (an ArtifactLog). A store without
        # one cannot record them: a step that returns artifacts then fails.
        self._artifacts = artifacts
        # The work items of external steps (a WorkItems). A store without them
        # cannot run external steps: such a step then fails.
        self._items = items
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
            items=self._items,
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
        item = None
        call = step.run
        if is_external(step):
            # Open (or wait again on) the work item, unless one has been submitted:
            # then this run validates the submission.
            found = self._external(sm, task, step, claim_token)
            if found is None:
                return
            item, submission = found
            attempt = item["attempt"]

            def call(ctx: StepContext, _submission: Submission = submission) -> Any:
                return step.validate(ctx, _submission)

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
        spawn: SpawnPlan | None = None
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
                result = call(ctx)
            finally:
                stop_beat.set()
                beat.join()
            # Children are checked before anything of the result is written: a spawn
            # that does not validate (including one of the wrong type, however empty)
            # fails the step, and nothing is created.
            children = plan_children(result.spawn, self.config)
            if children:
                if getattr(self.repo, "spawner", None) is None:
                    raise RuntimeError("the active store cannot spawn child tasks")
                spawn = SpawnPlan(children, step=task["step"], step_run_id=run_id)
            if result.artifacts:
                self._record_artifacts(task, step, run_id, result.artifacts)
            if result.updates:
                self.repo.update_fields(task_id, result.updates, token=claim_token)
        except SubmissionRejected as rejected:
            if item is None:  # not a validator: an ordinary failure
                self._fail(sm, task, run_id, t0, claim_token, f"SubmissionRejected: {rejected}")
                return
            self.step_runs.finish(
                run_id, status="rejected", duration_ms=_ms_since(t0), error=rejected.reason
            )
            outcome = Rejected(
                rejected.reason,
                item["id"],
                item["attempt"],
                item["max_attempts"],
                step_run_id=run_id,
            )
            after = sm.finish_step(task_id, outcome, token=claim_token)
            status = after["task_status"] if after else None
            self._notify(
                after or task,
                f"step {task['step']}: submission of item {item['id']} rejected (attempt "
                f"{item['attempt']} of {item['max_attempts']})"
                + ("; needs attention" if status == TaskStatus.MANUAL_REQUIRED else ""),
            )
            return
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
            self._fail(sm, task, run_id, t0, claim_token, f"{type(exc).__name__}: {exc}")
            return
        self.step_runs.finish(
            run_id,
            status="degraded" if result.degraded else "ok",
            duration_ms=_ms_since(t0),
            note=result.message or None,
        )
        outcome = (
            Succeeded(result.degraded)
            if item is None
            else Succeeded(result.degraded, item_id=item["id"], step_run_id=run_id)
        )
        if spawn is not None:
            outcome = replace(outcome, step_run_id=run_id, spawn=spawn)
            try:
                after = sm.finish_step(task_id, outcome, token=claim_token)
            except Exception as exc:  # noqa: BLE001 - the children could not be written
                # The transaction rolled back (no child, the parent not advanced): fail
                # the step instead of leaving the task running. Its updates and
                # artifacts were already written, as for any step that fails late.
                self._fail(
                    sm,
                    task,
                    run_id,
                    t0,
                    claim_token,
                    f"cannot create the child tasks: {type(exc).__name__}",
                )
                return
        else:
            after = sm.finish_step(task_id, outcome, token=claim_token)
        self._report_success(task, after, result)
        if spawn is not None and (spawn.created or spawn.existing):
            self._notify(
                after or task,
                f"step {task['step']} spawned {len(spawn.created)} child task(s)"
                + (f" ({len(spawn.existing)} already spawned)" if spawn.existing else ""),
            )

    def _fail(
        self,
        sm: StateMachine,
        task: dict[str, Any],
        run_id: int,
        t0: float,
        token: str | None,
        message: str,
    ) -> None:
        self.step_runs.finish(run_id, status="failed", duration_ms=_ms_since(t0), error=message)
        after = sm.finish_step(
            task["id"],
            Failed(message, self.config.scheduler.retry_delay_seconds),
            token=token,
        )
        status = after["task_status"] if after else None
        verb = {
            TaskStatus.FAILED: "failed",
            TaskStatus.PAUSED: "failed, paused",
            TaskStatus.TERMINATED: "failed, terminated",
        }.get(status, "needs attention")
        self._notify(after or task, f"step {task['step']} raised ({verb}): {message}")

    def _external(
        self, sm: StateMachine, task: dict[str, Any], step: Any, token: str | None
    ) -> tuple[dict[str, Any], Submission] | None:
        """For an external step: return the submitted item to validate, or open a
        work item (or wait again on the active one) and return ``None``."""
        task_id, name = task["id"], task["step"]

        def fail(message: str) -> None:
            after = sm.finish_step(
                task_id, Failed(message, self.config.scheduler.retry_delay_seconds), token=token
            )
            self._notify(after or task, f"step {name} could not open a work item: {message}")

        if self._items is None:
            fail("the active store has no work items (needed by external steps)")
            return None
        active = self._items.active_for(task_id)
        if active is not None and active["step"] != name:
            fail(f"work item {active['id']} of step {active['step']!r} is still active")
            return None
        if active is not None and active["status"] == "submitted":
            submission = Submission(
                item_id=active["id"],
                attempt=active["attempt"],
                value=active["submission"] or {},
                submitted_by=active["submitted_by"],
            )
            return active, submission
        if active is not None:  # open or claimed: keep waiting on it
            outcome = AwaitsExternal(active["attempt"], active["max_attempts"], reuse=True)
            sm.finish_step(task_id, outcome, token=token)
            return None
        max_attempts = getattr(step, "max_attempts", None)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts < 1:
            fail(f"max_attempts must be a positive integer, not {max_attempts!r}")
            return None
        previous = self._items.latest_for(task_id, name)
        if previous is not None and previous["status"] == "rejected":
            attempt, previous_error = previous["attempt"] + 1, previous["error"]
        else:  # first time here, or starting over after manual_required / retry
            attempt, previous_error = 1, None
        ctx = StepContext(
            task=task,
            config=self.config,
            should_stop=lambda: self._stop_requested(task_id),
            heartbeat=lambda progress=None: None,
            attempt=attempt,
        )
        try:
            request = step.request(ctx)
            kind, instructions = request.kind, request.instructions
            payload = dict(request.payload)
            json.dumps(payload)  # must be a JSON object
            if not isinstance(kind, str) or not isinstance(instructions, str):
                raise TypeError("kind and instructions must be strings")
        except Exception as exc:  # noqa: BLE001 - a broken request fails the step
            fail(f"{type(exc).__name__}: {exc}")
            return None
        outcome = AwaitsExternal(
            attempt,
            max_attempts,
            kind=kind,
            instructions=instructions,
            payload=payload,
            previous_error=previous_error,
        )
        after = sm.finish_step(task_id, outcome, token=token)
        if after is not None and after["task_status"] == TaskStatus.AWAITING_EXTERNAL:
            self._notify(
                after, f"step {name} waits for external work (attempt {attempt} of {max_attempts})"
            )
        return None

    def _admit(
        self, sm: StateMachine, task: dict[str, Any], step: Any, attempt: int, token: str | None
    ) -> bool:
        """Ask the gate whether the step may run now; resolve the task if not.

        A step refused, deferred or held for approval does not run and records no
        step run, so the attempt it is approved for is the one that runs next.
        """
        request = GateRequest(MappingProxyType(dict(task)), step, attempt)
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
            self._notify(after, f"step {task['step']} refused: {decision.reason}")
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

    def _record_artifacts(
        self, task: dict[str, Any], step: Any, run_id: int, artifacts: Any
    ) -> None:
        """Validate, hash and store a step's artifacts; raise (failing the step) if any
        is invalid. They are tied to this run, so if the run does not end ``ok`` or
        ``degraded`` readers skip them."""
        if self._artifacts is None:
            raise RuntimeError("the active store does not record artifacts")
        contract = contract_of(step)
        recorded = prepare_artifacts(
            list(artifacts),
            step=task["step"],
            provides=contract.provides if contract.declared else None,
            workspace_root=Path(self.config.workspace_root),
        )
        self._artifacts.record(task["id"], task["step"], recorded, step_run_id=run_id)

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
