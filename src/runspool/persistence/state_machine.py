"""Task state machine: all state-transition rules live here.

State diagram (happy path advances step by step; failures branch to retry or
manual_required once retries are exhausted)::

    queued --claim--> running --complete_step--> queued (next step)
                              \\--complete_step--> completed (last step)
                              \\--defer--------> queued (same step, no retry)
                              \\--fail---------> failed         (retries left)
                              \\--fail---------> manual_required (retries exhausted)

    running --request_pause--> pause_pending --apply_pause--> paused --resume--> queued
    *       --request_terminate--> (flag) --apply_terminate--> terminated
    failed / manual_required --retry--> queued
"""

from __future__ import annotations

from runspool.clock import utcnow_plus_text, utcnow_text
from runspool.models import TERMINAL_STATUSES, EventType, TaskStatus, WorkflowDef
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository
from runspool.persistence.step_run_log import StepRunLog


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
        # still-"running" step_run row so it does not hang forever.
        self.step_runs = step_runs

    def _close_running_step_run(self, task_id: int) -> None:
        if self.step_runs is not None:
            self.step_runs.close_running_for_task(task_id)

    def _task_or_missing(self, task_id: int) -> dict:
        task = self.repo.get_task(task_id)
        if task is None:
            raise KeyError(task_id)
        return task

    def claim(self, task_id: int, *, worker: str) -> bool:
        # Atomic claim: the repository runs a single conditional UPDATE guarded by
        # ``task_status='queued'`` and reports via rowcount whether this caller
        # won. This is safe even when the daemon and a CLI process race for the
        # same task; check-then-act could otherwise let two callers both claim it.
        task = self.repo.get_task(task_id)
        if task is None:
            return False
        if not self.repo.claim_queued(task_id, worker=worker, now=utcnow_text()):
            return False
        self.log.add(task_id, EventType.CLAIMED, step=task["step"], message=f"claimed by {worker}")
        return True

    def complete_step(self, task_id: int) -> None:
        task = self.repo.get_task(task_id)
        if task is None:
            return
        nxt = self.workflow.next_step(task["step"])
        if nxt.done:
            self.repo.update_fields(
                task_id,
                {
                    "task_status": TaskStatus.COMPLETED,
                    "locked_by": None,
                    "locked_at": None,
                    "heartbeat_at": None,
                },
            )
            self.log.add(
                task_id, EventType.COMPLETED, step=task["step"], message="workflow completed"
            )
            return
        self.repo.update_fields(
            task_id,
            {
                "step": nxt.step,
                "task_status": TaskStatus.QUEUED,
                "locked_by": None,
                "locked_at": None,
                "heartbeat_at": None,
            },
        )
        self.log.add(
            task_id, EventType.STEP_COMPLETED, step=task["step"], message=f"advanced to {nxt.step}"
        )

    def defer(self, task_id: int) -> None:
        # Step not ready: stay on the current step, clear the lock, requeue and
        # retry next tick. Does not count as a retry.
        task = self.repo.get_task(task_id)
        if task is None:
            return
        self.repo.update_fields(
            task_id,
            {
                "task_status": TaskStatus.QUEUED,
                "locked_by": None,
                "locked_at": None,
                "heartbeat_at": None,
            },
        )
        self.log.add(
            task_id, EventType.DEFERRED, step=task["step"], message="not ready, will retry"
        )

    def skip_step(self, task_id: int) -> None:
        # Conditional skip for when()==False: the task is still QUEUED (unclaimed),
        # so advance straight to the next step without touching lock fields. The
        # event is marked as a skip to distinguish it from a completed run.
        task = self.repo.get_task(task_id)
        if task is None:
            return
        nxt = self.workflow.next_step(task["step"])
        if nxt.done:
            self.repo.update_fields(task_id, {"task_status": TaskStatus.COMPLETED})
            self.log.add(
                task_id, EventType.COMPLETED, step=task["step"], message="last step skipped"
            )
            return
        self.repo.update_fields(task_id, {"step": nxt.step})
        self.log.add(
            task_id,
            EventType.STEP_COMPLETED,
            step=task["step"],
            message=f"skipped, advanced to {nxt.step}",
        )

    def fail(self, task_id: int, error: str, *, retry_delay_seconds: int = 0) -> None:
        task = self.repo.get_task(task_id)
        if task is None:
            return
        retry_count = task["retry_count"] + 1
        if retry_count > task["max_retries"]:
            self.repo.update_fields(
                task_id,
                {
                    "task_status": TaskStatus.MANUAL_REQUIRED,
                    "retry_count": retry_count,
                    "last_error": error,
                    "next_retry_at": None,
                    "locked_by": None,
                    "locked_at": None,
                    "heartbeat_at": None,
                },
            )
            self.log.add(task_id, EventType.MANUAL_REQUIRED, step=task["step"], message=error)
            return
        # Schedule an automatic retry. The task stays FAILED (observable) until
        # next_retry_at passes, at which point the coordinator requeues it.
        self.repo.update_fields(
            task_id,
            {
                "task_status": TaskStatus.FAILED,
                "retry_count": retry_count,
                "last_error": error,
                "next_retry_at": utcnow_plus_text(retry_delay_seconds),
                "locked_by": None,
                "locked_at": None,
                "heartbeat_at": None,
            },
        )
        self.log.add(task_id, EventType.STEP_FAILED, step=task["step"], message=error)

    def requeue_failed(self, task_id: int) -> None:
        """Move a FAILED task back to QUEUED for its scheduled automatic retry.

        Guarded so a task that changed state between selection and requeue (e.g.
        a manual retry or terminate) is not disturbed.
        """
        task = self.repo.get_task(task_id)
        if task is None or task["task_status"] != TaskStatus.FAILED:
            return
        self.repo.update_fields(
            task_id, {"task_status": TaskStatus.QUEUED, "next_retry_at": None}
        )
        self.log.add(
            task_id, EventType.RETRY, step=task["step"], message="automatic retry"
        )

    def request_pause(self, task_id: int) -> None:
        task = self._task_or_missing(task_id)
        status = task["task_status"]
        if status not in (TaskStatus.QUEUED, TaskStatus.RUNNING):
            raise IllegalTransition(task_id, status, "pause", allowed="queued or running")
        if status == TaskStatus.RUNNING:
            self.repo.update_fields(
                task_id, {"task_status": TaskStatus.PAUSE_PENDING, "pause_requested": 1}
            )
        else:
            self.repo.update_fields(task_id, {"task_status": TaskStatus.PAUSED})
        self.log.add(task_id, EventType.PAUSE_REQUESTED, step=task["step"])

    def apply_pause(self, task_id: int) -> None:
        # Pause in place (same step). Used for crash recovery, where the step was
        # interrupted before finishing, so re-running it on resume is correct.
        # For a step that finished successfully, use pause_after_successful_step.
        if self.repo.get_task(task_id) is None:
            return
        self.repo.update_fields(
            task_id,
            {
                "task_status": TaskStatus.PAUSED,
                "pause_requested": 0,
                "locked_by": None,
                "locked_at": None,
                "heartbeat_at": None,
            },
        )
        self.log.add(task_id, EventType.PAUSED)

    def pause_after_successful_step(self, task_id: int) -> None:
        """Apply a pause requested while a step was running, at the step boundary.

        The step just completed successfully, so we must NOT leave the task on
        that step (resuming would re-run an already-done, possibly side-effectful
        step). Instead advance to the next step and pause there; if that was the
        last step there is nothing to pause before, so complete the workflow.
        """
        task = self.repo.get_task(task_id)
        if task is None:
            return
        nxt = self.workflow.next_step(task["step"])
        if nxt.done:
            self.repo.update_fields(
                task_id,
                {
                    "task_status": TaskStatus.COMPLETED,
                    "pause_requested": 0,
                    "locked_by": None,
                    "locked_at": None,
                    "heartbeat_at": None,
                },
            )
            self.log.add(
                task_id, EventType.COMPLETED, step=task["step"], message="workflow completed"
            )
            return
        self.repo.update_fields(
            task_id,
            {
                "step": nxt.step,
                "task_status": TaskStatus.PAUSED,
                "pause_requested": 0,
                "locked_by": None,
                "locked_at": None,
                "heartbeat_at": None,
            },
        )
        self.log.add(
            task_id, EventType.STEP_COMPLETED, step=task["step"], message=f"advanced to {nxt.step}"
        )
        self.log.add(task_id, EventType.PAUSED, step=nxt.step)

    def request_terminate(self, task_id: int) -> None:
        task = self._task_or_missing(task_id)
        status = task["task_status"]
        if status in TERMINAL_STATUSES:
            raise IllegalTransition(
                task_id, status, "terminate", allowed="any non-terminal state"
            )
        if status == TaskStatus.RUNNING:
            self.repo.update_fields(task_id, {"terminate_requested": 1})
        else:
            self.repo.update_fields(task_id, {"task_status": TaskStatus.TERMINATED})
        self.log.add(task_id, EventType.TERMINATE_REQUESTED, step=task["step"])

    def apply_terminate(self, task_id: int) -> None:
        if self.repo.get_task(task_id) is None:
            return
        self.repo.update_fields(
            task_id,
            {
                "task_status": TaskStatus.TERMINATED,
                "terminate_requested": 0,
                "locked_by": None,
                "locked_at": None,
                "heartbeat_at": None,
            },
        )
        self.log.add(task_id, EventType.TERMINATED)

    def resume(self, task_id: int) -> None:
        task = self._task_or_missing(task_id)
        status = task["task_status"]
        if status != TaskStatus.PAUSED:
            raise IllegalTransition(task_id, status, "resume", allowed="paused")
        # Defensively clear pause_requested so no stale pause signal survives.
        self.repo.update_fields(
            task_id, {"task_status": TaskStatus.QUEUED, "pause_requested": 0}
        )
        self.log.add(task_id, EventType.RESUMED)

    def retry(self, task_id: int) -> None:
        # retry only clears the error and requeues from the current step;
        # retry_count is maintained by fail() and not reset here. Human override
        # of an exhausted manual_required task (raising the cap) is provided by
        # the CLI's set-retries command.
        task = self._task_or_missing(task_id)
        status = task["task_status"]
        if status not in (TaskStatus.FAILED, TaskStatus.MANUAL_REQUIRED):
            raise IllegalTransition(
                task_id, status, "retry", allowed="failed or manual_required"
            )
        self.repo.update_fields(
            task_id, {"task_status": TaskStatus.QUEUED, "last_error": None, "next_retry_at": None}
        )
        self.log.add(task_id, EventType.RETRY)

    def recover_interrupted(self) -> None:
        for task in self.repo.list_by_status(TaskStatus.RUNNING):
            self._close_running_step_run(task["id"])
            self.repo.update_fields(
                task["id"],
                {
                    "task_status": TaskStatus.QUEUED,
                    "locked_by": None,
                    "locked_at": None,
                    "heartbeat_at": None,
                },
            )
        for task in self.repo.list_by_status(TaskStatus.PAUSE_PENDING):
            self._close_running_step_run(task["id"])
            self.repo.update_fields(
                task["id"], {"task_status": TaskStatus.PAUSED, "pause_requested": 0}
            )

    def requeue_stale(self, task_id: int) -> None:
        # Guard against TOCTOU: between listing stale tasks and reclaiming, a
        # worker may have finished; only reclaim if still RUNNING.
        task = self.repo.get_task(task_id)
        if task is None or task["task_status"] != TaskStatus.RUNNING:
            return
        self._close_running_step_run(task_id)
        self.repo.update_fields(
            task_id,
            {
                "task_status": TaskStatus.QUEUED,
                "locked_by": None,
                "locked_at": None,
                "heartbeat_at": None,
            },
        )
        self.log.add(task_id, EventType.RECLAIMED, message="heartbeat timeout, requeued")
