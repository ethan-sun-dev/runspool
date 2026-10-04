"""Task state machine: all state-transition rules live here.

State diagram (happy path advances step by step; failures branch to retry or
manual_required once retries are exhausted)::

    queued --claim--> running --(step succeeded)--> queued (next step)
                              \\--(step succeeded)--> completed | partially_completed
                              \\--(step deferred)--> queued (same step, no retry;
                                                     not before next_retry_at)
                              \\--(step failed)----> failed (retries left; requeued
                                                     automatically at next_retry_at)
                              \\--(step failed)----> manual_required (retries exhausted)

    running --request_pause--> pause_pending --(step boundary)--> paused --resume--> queued
    *       --request_terminate--> (flag) --(step boundary)--> terminated
    failed / manual_required --retry--> queued
    queued (deferred, waiting) --wake--> queued (runnable now)

Every write is a compare-and-set through ``TaskRepository.transition``: it applies
only if the task is still in the state this machine read, and it records the
transition's events in the same transaction. A concurrent change makes the write a
no-op, and the machine re-reads and decides again.

At a step boundary the runner reports the step's outcome to :meth:`finish_step`,
which decides the transition in one place: terminate wins over everything; a
requested pause applies after the outcome (a finished step advances first, a
deferred step pauses in place, a failed step records the failure and pauses); and,
given the claim token, a stale worker whose claim was reclaimed changes nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from runspool.clock import utcnow_plus_text, utcnow_text
from runspool.models import TERMINAL_STATUSES, EventType, TaskStatus, WorkflowDef
from runspool.persistence.event_log import Event, EventLog
from runspool.persistence.repository import TaskRepository
from runspool.persistence.step_run_log import StepRunLog

_UNLOCK: dict[str, Any] = {
    "locked_by": None,
    "locked_at": None,
    "heartbeat_at": None,
    "claim_token": None,
}
_EXECUTING = (TaskStatus.RUNNING, TaskStatus.PAUSE_PENDING)
_ALL = tuple(TaskStatus)
_ATTEMPTS = 5
_UNREAD = object()


@dataclass(frozen=True)
class Succeeded:
    """The step returned. ``degraded``: it ran but did not fully do its job."""

    degraded: bool = False


@dataclass(frozen=True)
class Deferred:
    """The step is not ready: stay on it; try again after ``delay_seconds``."""

    reason: str = "not ready, will retry"
    delay_seconds: int = 0


@dataclass(frozen=True)
class Failed:
    """The step raised ``error``."""

    error: str
    retry_delay_seconds: int = 0


Outcome = Succeeded | Deferred | Failed


class IllegalTransition(Exception):
    """A user-initiated control action is not valid for the task's current state.

    Raised by the state machine so the guard lives in one place rather than only
    in the CLI. Carries the task id, its current status, and the attempted action.
    """

    def __init__(self, task_id: int, status: str, action: str, *, allowed: str) -> None:
        super().__init__(
            f"cannot {action} task {task_id}: it is {status} (allowed when: {allowed})"
        )
        self.task_id = task_id
        self.status = status
        self.action = action


class StateMachine:
    def __init__(
        self,
        repo: TaskRepository,
        log: EventLog,
        *,
        workflow: WorkflowDef,
        step_runs: StepRunLog | None = None,
    ) -> None:
        self.repo = repo
        self.log = log
        self.workflow = workflow
        # Optional: when present, recovery/reclaim closes the interrupted step's
        # still-"running" step_run row, and the end of a workflow can tell whether
        # any step ran degraded.
        self.step_runs = step_runs

    # -- helpers ------------------------------------------------------------------

    def _move(
        self,
        task_id: int,
        fields: dict[str, Any],
        *,
        expect: tuple[TaskStatus, ...],
        require: dict[str, Any] | None = None,
        events: tuple[Event, ...] | list[Event] = (),
    ) -> bool:
        return self.repo.transition(
            task_id, fields, expect=expect, require=require, events=events
        )

    def _task_or_missing(self, task_id: int) -> dict:
        task = self.repo.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def _close_running_step_run(self, task_id: int) -> None:
        if self.step_runs is not None:
            self.step_runs.close_running_for_task(task_id)

    def _final_status(self, task_id: int, degraded: bool) -> TaskStatus:
        if degraded or (self.step_runs is not None and self.step_runs.has_degraded(task_id)):
            return TaskStatus.PARTIALLY_COMPLETED
        return TaskStatus.COMPLETED

    def _user_action(self, task_id: int, decide) -> None:
        """Apply a user action as a compare-and-set, re-deciding on contention."""
        for _ in range(_ATTEMPTS):
            task = self._task_or_missing(task_id)
            fields, events = decide(task)  # raises IllegalTransition when not allowed
            if self._move(task_id, fields, expect=(task["task_status"],), events=events):
                return
        raise RuntimeError(f"task {task_id} kept changing; action not applied")

    # -- claiming -----------------------------------------------------------------

    def claim(self, task_id: int, *, worker: str, token: str | None = None) -> bool:
        # Atomic claim: a single conditional UPDATE guarded by task_status='queued'
        # reports via rowcount whether this caller won, even when the daemon and a
        # CLI process race for the same task.
        task = self.repo.get_task(task_id)
        if task is None:
            return False
        if task["task_status"] == TaskStatus.QUEUED and task["terminate_requested"]:
            # A terminate request must never be outlived by another run of the step.
            self._move(
                task_id,
                {"task_status": TaskStatus.TERMINATED, "terminate_requested": 0, **_UNLOCK},
                expect=(TaskStatus.QUEUED,),
                require={"terminate_requested": 1},
                events=[Event(EventType.TERMINATED, step=task["step"])],
            )
            return False
        event = Event(EventType.CLAIMED, step=task["step"], message=f"claimed by {worker}")
        return self.repo.claim_queued(
            task_id, worker=worker, now=utcnow_text(), token=token, event=event
        )

    # -- step boundary ------------------------------------------------------------

    def finish_step(
        self, task_id: int, outcome: Outcome, *, token: str | None = None
    ) -> dict[str, Any] | None:
        """Resolve a finished step run. Returns the task as it is afterwards.

        Reads the pause/terminate flags, decides the transition (see the module
        docstring) and writes it as a compare-and-set that also requires the flags
        (and the claim token, if given) to be unchanged. On contention it re-reads
        and decides again. A task that is no longer executing, or whose claim token
        differs, is left alone: another writer already resolved it.
        """
        for _ in range(_ATTEMPTS):
            task = self.repo.get_task(task_id)
            if task is None or task["task_status"] not in _EXECUTING:
                return task
            if token is not None and task["claim_token"] != token:
                return task
            require = {
                "pause_requested": task["pause_requested"],
                "terminate_requested": task["terminate_requested"],
                # The failure path decides from these; a concurrent set-retries must
                # make it decide again rather than be overwritten.
                "retry_count": task["retry_count"],
                "max_retries": task["max_retries"],
            }
            if token is not None:
                require["claim_token"] = token
            fields, events = self._boundary(
                task,
                outcome,
                terminate=bool(task["terminate_requested"]),
                pause=bool(task["pause_requested"]),
            )
            if self._move(
                task_id, fields, expect=(task["task_status"],), require=require, events=events
            ):
                return self.repo.get_task(task_id)
        return self.repo.get_task(task_id)

    def _boundary(
        self, task: dict[str, Any], outcome: Outcome, *, terminate: bool, pause: bool
    ) -> tuple[dict[str, Any], list[Event]]:
        step = task["step"]
        if terminate:
            fields = {
                "task_status": TaskStatus.TERMINATED,
                "terminate_requested": 0,
                # Terminate wins: a terminal task must not carry a stale pause signal.
                "pause_requested": 0,
                "next_retry_at": None,
                **_UNLOCK,
            }
            if isinstance(outcome, Failed):
                fields["last_error"] = outcome.error
            return fields, [Event(EventType.TERMINATED, step=step)]

        if isinstance(outcome, Succeeded):
            nxt = self.workflow.next_step(step)
            if nxt.done:
                status = self._final_status(task["id"], outcome.degraded)
                message = (
                    "workflow completed"
                    if status == TaskStatus.COMPLETED
                    else "workflow completed; some steps were degraded"
                )
                return (
                    {"task_status": status, "pause_requested": 0, **_UNLOCK},
                    [Event(EventType.COMPLETED, step=step, message=message)],
                )
            advanced = Event(EventType.STEP_COMPLETED, step=step, message=f"advanced to {nxt.step}")
            if pause:
                # The step finished: advance first so it is not re-run on resume.
                return (
                    {
                        "step": nxt.step,
                        "task_status": TaskStatus.PAUSED,
                        "pause_requested": 0,
                        **_UNLOCK,
                    },
                    [advanced, Event(EventType.PAUSED, step=nxt.step)],
                )
            return ({"step": nxt.step, "task_status": TaskStatus.QUEUED, **_UNLOCK}, [advanced])

        if isinstance(outcome, Deferred):
            deferred = Event(EventType.DEFERRED, step=step, message=outcome.reason)
            if pause:
                # The step did not run to completion: pause in place, re-run on resume.
                return (
                    {"task_status": TaskStatus.PAUSED, "pause_requested": 0, **_UNLOCK},
                    [deferred, Event(EventType.PAUSED, step=step)],
                )
            not_before = (
                utcnow_plus_text(outcome.delay_seconds) if outcome.delay_seconds > 0 else None
            )
            return (
                {"task_status": TaskStatus.QUEUED, "next_retry_at": not_before, **_UNLOCK},
                [deferred],
            )

        retry_count = task["retry_count"] + 1
        if retry_count > task["max_retries"]:
            return (
                {
                    "task_status": TaskStatus.MANUAL_REQUIRED,
                    "retry_count": retry_count,
                    "last_error": outcome.error,
                    "next_retry_at": None,
                    "pause_requested": 0,
                    **_UNLOCK,
                },
                [Event(EventType.MANUAL_REQUIRED, step=step, message=outcome.error)],
            )
        failed = Event(EventType.STEP_FAILED, step=step, message=outcome.error)
        if pause:
            # Record the failure, but honour the pause instead of scheduling a retry.
            return (
                {
                    "task_status": TaskStatus.PAUSED,
                    "retry_count": retry_count,
                    "last_error": outcome.error,
                    "next_retry_at": None,
                    "pause_requested": 0,
                    **_UNLOCK,
                },
                [failed, Event(EventType.PAUSED, step=step)],
            )
        # Schedule an automatic retry. The task stays FAILED (observable) until
        # next_retry_at passes, at which point the coordinator requeues it.
        return (
            {
                "task_status": TaskStatus.FAILED,
                "retry_count": retry_count,
                "last_error": outcome.error,
                "next_retry_at": utcnow_plus_text(outcome.retry_delay_seconds),
                **_UNLOCK,
            },
            [failed],
        )

    def _force(self, task_id: int, outcome: Outcome, *, terminate: bool, pause: bool) -> None:
        """Apply one specific boundary transition to an executing task, flags aside."""
        for _ in range(_ATTEMPTS):
            task = self.repo.get_task(task_id)
            if task is None or task["task_status"] not in _EXECUTING:
                return
            fields, events = self._boundary(task, outcome, terminate=terminate, pause=pause)
            if self._move(task_id, fields, expect=(task["task_status"],), events=events):
                return

    def complete_step(self, task_id: int, *, degraded: bool = False) -> None:
        self._force(task_id, Succeeded(degraded), terminate=False, pause=False)

    def defer(
        self, task_id: int, reason: str = "not ready, will retry", *, delay_seconds: int = 0
    ) -> None:
        self._force(task_id, Deferred(reason, delay_seconds), terminate=False, pause=False)

    def fail(self, task_id: int, error: str, *, retry_delay_seconds: int = 0) -> None:
        self._force(task_id, Failed(error, retry_delay_seconds), terminate=False, pause=False)

    def pause_after_successful_step(self, task_id: int) -> None:
        """Pause at the boundary of a step that finished: advance first, then pause."""
        self._force(task_id, Succeeded(), terminate=False, pause=True)

    def apply_terminate(self, task_id: int) -> None:
        self._force(task_id, Succeeded(), terminate=True, pause=False)

    def apply_pause(self, task_id: int) -> None:
        """Pause in place (same step); for a step that did not finish."""
        for _ in range(_ATTEMPTS):
            task = self.repo.get_task(task_id)
            if task is None or task["task_status"] not in _EXECUTING:
                return
            if self._move(
                task_id,
                {"task_status": TaskStatus.PAUSED, "pause_requested": 0, **_UNLOCK},
                expect=(task["task_status"],),
                events=[Event(EventType.PAUSED, step=task["step"])],
            ):
                return

    # -- scheduler paths ----------------------------------------------------------

    def skip_step(self, task_id: int) -> None:
        # Conditional skip for when()==False: the task is still QUEUED (unclaimed),
        # so advance straight to the next step. The event is marked as a skip to
        # distinguish it from a completed run.
        task = self.repo.get_task(task_id)
        if task is None or task["task_status"] != TaskStatus.QUEUED:
            return
        nxt = self.workflow.next_step(task["step"])
        if nxt.done:
            self._move(
                task_id,
                {"task_status": self._final_status(task_id, False)},
                expect=(TaskStatus.QUEUED,),
                require={"step": task["step"]},
                events=[Event(EventType.COMPLETED, step=task["step"], message="last step skipped")],
            )
            return
        self._move(
            task_id,
            {"step": nxt.step},
            expect=(TaskStatus.QUEUED,),
            require={"step": task["step"]},
            events=[
                Event(
                    EventType.STEP_COMPLETED,
                    step=task["step"],
                    message=f"skipped, advanced to {nxt.step}",
                )
            ],
        )

    def requeue_failed(self, task_id: int) -> None:
        """Move a FAILED task back to QUEUED for its scheduled automatic retry.

        A task that changed state between selection and requeue (e.g. a manual
        retry or terminate) is not disturbed: the compare-and-set does not match.
        """
        task = self.repo.get_task(task_id)
        if task is None:
            return
        self._move(
            task_id,
            {"task_status": TaskStatus.QUEUED, "next_retry_at": None},
            expect=(TaskStatus.FAILED,),
            events=[Event(EventType.RETRY, step=task["step"], message="automatic retry")],
        )

    def recover_interrupted(self) -> None:
        """After a crash, nothing is executing: settle every RUNNING / PAUSE_PENDING task."""
        for status in _EXECUTING:
            for task in self.repo.list_by_status(status):
                self._settle(task["id"], reason="interrupted", check_alive=False)

    def requeue_stale(self, task_id: int) -> None:
        """Settle a task whose worker stopped heartbeating (RUNNING or PAUSE_PENDING).

        If its heartbeat moves while we decide, the worker is alive after all and
        the task is left alone. Clearing the claim token makes the stale worker's
        eventual finish_step a no-op.
        """
        self._settle(task_id, reason="heartbeat timeout", check_alive=True)

    def _settle(self, task_id: int, *, reason: str, check_alive: bool) -> bool:
        """Release an executing task whose worker is gone. The interrupted step is
        assumed not done: a requested terminate wins; a requested pause (or a task
        that was already pausing) pauses in place, so the step re-runs on resume;
        otherwise the task is requeued on the same step. Re-decides on contention."""
        seen_heartbeat: Any = _UNREAD
        for _ in range(_ATTEMPTS):
            task = self.repo.get_task(task_id)
            if task is None or task["task_status"] not in _EXECUTING:
                return False
            if seen_heartbeat is _UNREAD:
                seen_heartbeat = task["heartbeat_at"]
            elif check_alive and task["heartbeat_at"] != seen_heartbeat:
                return False  # it heartbeated while we were deciding: alive after all
            status, step = task["task_status"], task["step"]
            if task["terminate_requested"]:
                fields = {
                    "task_status": TaskStatus.TERMINATED,
                    "terminate_requested": 0,
                    "pause_requested": 0,
                    **_UNLOCK,
                }
                event = Event(EventType.TERMINATED, step=step, message=reason)
            elif task["pause_requested"] or status == TaskStatus.PAUSE_PENDING:
                fields = {"task_status": TaskStatus.PAUSED, "pause_requested": 0, **_UNLOCK}
                event = Event(EventType.PAUSED, step=step, message=reason)
            else:
                fields = {"task_status": TaskStatus.QUEUED, **_UNLOCK}
                event = Event(EventType.RECLAIMED, step=step, message=f"{reason}, requeued")
            require = {
                "pause_requested": task["pause_requested"],
                "terminate_requested": task["terminate_requested"],
                "claim_token": task["claim_token"],
            }
            if check_alive:
                require["heartbeat_at"] = task["heartbeat_at"]
            if self._move(task_id, fields, expect=(status,), require=require, events=[event]):
                self._close_running_step_run(task_id)
                return True
        return False

    # -- user actions -------------------------------------------------------------

    def request_pause(self, task_id: int) -> None:
        def decide(task):
            status = task["task_status"]
            if status not in (TaskStatus.QUEUED, TaskStatus.RUNNING):
                raise IllegalTransition(task_id, status, "pause", allowed="queued or running")
            event = Event(EventType.PAUSE_REQUESTED, step=task["step"])
            if status == TaskStatus.RUNNING:
                return {"task_status": TaskStatus.PAUSE_PENDING, "pause_requested": 1}, [event]
            return {"task_status": TaskStatus.PAUSED, "next_retry_at": None}, [event]

        self._user_action(task_id, decide)

    def request_terminate(self, task_id: int) -> None:
        def decide(task):
            status = task["task_status"]
            if status in TERMINAL_STATUSES:
                raise IllegalTransition(
                    task_id, status, "terminate", allowed="any non-terminal state"
                )
            event = Event(EventType.TERMINATE_REQUESTED, step=task["step"])
            # RUNNING and PAUSE_PENDING both mean a worker is mid-step: set the flag
            # and let the step boundary apply it (terminate wins over a pending
            # pause there). Writing TERMINATED directly would race the worker.
            if status in _EXECUTING:
                return {"terminate_requested": 1}, [event]
            return {"task_status": TaskStatus.TERMINATED, "next_retry_at": None}, [
                event,
                Event(EventType.TERMINATED, step=task["step"]),
            ]

        self._user_action(task_id, decide)

    def resume(self, task_id: int) -> None:
        def decide(task):
            status = task["task_status"]
            if status != TaskStatus.PAUSED:
                raise IllegalTransition(task_id, status, "resume", allowed="paused")
            # Defensively clear pause_requested so no stale pause signal survives.
            fields = {"task_status": TaskStatus.QUEUED, "pause_requested": 0, "next_retry_at": None}
            return fields, [Event(EventType.RESUMED, step=task["step"])]

        self._user_action(task_id, decide)

    def retry(self, task_id: int) -> None:
        # retry clears the error and requeues from the current step; retry_count is
        # maintained by the failure path and not reset here (set-retries does that).
        def decide(task):
            status = task["task_status"]
            if status not in (TaskStatus.FAILED, TaskStatus.MANUAL_REQUIRED):
                raise IllegalTransition(
                    task_id, status, "retry", allowed="failed or manual_required"
                )
            fields = {"task_status": TaskStatus.QUEUED, "last_error": None, "next_retry_at": None}
            return fields, [Event(EventType.RETRY, step=task["step"])]

        self._user_action(task_id, decide)

    def wake(self, task_id: int) -> None:
        """Make a deferred task runnable now instead of at its ``next_retry_at``."""

        def decide(task):
            status = task["task_status"]
            if status != TaskStatus.QUEUED or not task["next_retry_at"]:
                raise IllegalTransition(
                    task_id, status, "wake", allowed="queued and waiting on a delay"
                )
            return {"next_retry_at": None}, [Event(EventType.WOKEN, step=task["step"])]

        self._user_action(task_id, decide)

    def set_step(self, task_id: int, step: str, *, force: bool = False) -> None:
        if step not in self.workflow.steps:
            raise ValueError(f"step {step!r} is not part of workflow {self.workflow.name!r}")

        def decide(task):
            status = task["task_status"]
            # Moving a running/queued task mid-flight, or rewinding a finished one,
            # is a foot-gun; restrict to recovery states unless explicitly forced.
            if not force and status not in (TaskStatus.FAILED, TaskStatus.MANUAL_REQUIRED):
                raise IllegalTransition(
                    task_id, status, "set-step", allowed="failed or manual_required (use --force)"
                )
            if status in _EXECUTING or status in TERMINAL_STATUSES:
                raise IllegalTransition(
                    task_id,
                    status,
                    "set-step",
                    allowed="not running and not finished (even with --force)",
                )
            return {"step": step}, [
                Event(EventType.FIELD_SET, step=step, message=f"step set to {step}")
            ]

        self._user_action(task_id, decide)

    def set_retries(self, task_id: int, max_retries: int) -> None:
        # Mirror the config model's ge=0 constraint: a negative cap would route the
        # very first failure straight to manual_required, silently disabling retries.
        if max_retries < 0:
            raise ValueError(f"max-retries must be >= 0, got {max_retries}")

        def decide(task):
            return {"max_retries": max_retries, "retry_count": 0}, [
                Event(EventType.FIELD_SET, step=task["step"], message=f"max_retries={max_retries}")
            ]

        self._user_action(task_id, decide)
