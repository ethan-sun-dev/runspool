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

from runspool.clock import utcnow_text
from runspool.models import EventType, TaskStatus, WorkflowDef
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository


class StateMachine:
    def __init__(self, repo: TaskRepository, log: EventLog, *, workflow: WorkflowDef) -> None:
        self.repo = repo
        self.log = log
        self.workflow = workflow

    def claim(self, task_id: int, *, worker: str) -> bool:
        # Currently check-then-act (get then update). Safe here because only the
        # single-threaded Coordinator calls claim (worker threads only run
        # runner.execute). To add multiple coordinators or processes, switch to a
        # single atomic ``UPDATE ... WHERE id=? AND task_status='queued'`` and use
        # rowcount to decide whether the claim succeeded.
        task = self.repo.get_task(task_id)
        if task is None or task["task_status"] != TaskStatus.QUEUED:
            return False
        now = utcnow_text()
        self.repo.update_fields(
            task_id,
            {
                "task_status": TaskStatus.RUNNING,
                "locked_by": worker,
                "locked_at": now,
                "heartbeat_at": now,
            },
        )
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

    def fail(self, task_id: int, error: str) -> None:
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
                    "locked_by": None,
                    "locked_at": None,
                    "heartbeat_at": None,
                },
            )
            self.log.add(task_id, EventType.MANUAL_REQUIRED, step=task["step"], message=error)
            return
        self.repo.update_fields(
            task_id,
            {
                "task_status": TaskStatus.FAILED,
                "retry_count": retry_count,
                "last_error": error,
                "locked_by": None,
                "locked_at": None,
                "heartbeat_at": None,
            },
        )
        self.log.add(task_id, EventType.STEP_FAILED, step=task["step"], message=error)

    def request_pause(self, task_id: int) -> None:
        task = self.repo.get_task(task_id)
        if task is None:
            return
        if task["task_status"] == TaskStatus.RUNNING:
            self.repo.update_fields(
                task_id, {"task_status": TaskStatus.PAUSE_PENDING, "pause_requested": 1}
            )
        else:
            self.repo.update_fields(task_id, {"task_status": TaskStatus.PAUSED})
        self.log.add(task_id, EventType.PAUSE_REQUESTED, step=task["step"])

    def apply_pause(self, task_id: int) -> None:
        # Should only be called after request_pause, once the worker reaches a
        # safe point.
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

    def request_terminate(self, task_id: int) -> None:
        task = self.repo.get_task(task_id)
        if task is None:
            return
        if task["task_status"] == TaskStatus.RUNNING:
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
        self.repo.update_fields(
            task_id, {"task_status": TaskStatus.QUEUED, "last_error": None}
        )
        self.log.add(task_id, EventType.RETRY)

    def recover_interrupted(self) -> None:
        for task in self.repo.list_by_status(TaskStatus.RUNNING):
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
            self.repo.update_fields(
                task["id"], {"task_status": TaskStatus.PAUSED, "pause_requested": 0}
            )

    def requeue_stale(self, task_id: int) -> None:
        # Guard against TOCTOU: between listing stale tasks and reclaiming, a
        # worker may have finished; only reclaim if still RUNNING.
        task = self.repo.get_task(task_id)
        if task is None or task["task_status"] != TaskStatus.RUNNING:
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
        self.log.add(task_id, EventType.RECLAIMED, message="heartbeat timeout, requeued")
