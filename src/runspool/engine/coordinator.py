"""Coordinator: lightweight scheduler. Each tick picks queued tasks and, by
per-step quota and the step's when() guard, either skips or claims and submits
them to the worker pool."""

from __future__ import annotations

from typing import Any

from runspool.engine.registry import StepRegistry
from runspool.engine.runner import TaskRunner
from runspool.engine.worker_pool import WorkerPool
from runspool.models import TaskStatus
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository
from runspool.persistence.state_machine import StateMachine


class Coordinator:
    def __init__(
        self,
        repo: TaskRepository,
        log: EventLog,
        registry: StepRegistry,
        runner: TaskRunner,
        pool: WorkerPool,
        config: Any,
    ) -> None:
        self.repo = repo
        self.log = log
        self.registry = registry
        self.runner = runner
        self.pool = pool
        self.config = config

    def tick(self) -> int:
        """Run one scheduling round. Returns the number of tasks submitted to the
        worker pool this round (a skip or an auto-requeue counts as progress)."""
        progressed = self._requeue_due_failures()

        # Current per-step running count (DB is the source of truth), incremented
        # in memory as this tick claims more.
        load: dict[str, int] = {}
        for task in self.repo.list_by_status(TaskStatus.RUNNING):
            load[task["step"]] = load.get(task["step"], 0) + 1

        for task in self.repo.list_by_status(TaskStatus.QUEUED):
            step_name = task["step"]
            if load.get(step_name, 0) >= self.config.step_quota(step_name):
                continue
            sm = self._state_machine_for(task)
            if sm is None:  # unknown workflow: skip this task, do not break the tick
                continue
            try:
                step = self.registry.get(step_name)
            except KeyError:
                # Unregistered step: skip this task so one bad task cannot stall
                # the rest of the round.
                continue
            if not step.when(task, self.config):
                sm.skip_step(task["id"])
                progressed += 1
                continue
            if sm.claim(task["id"], worker=f"pool-{task['id']}"):
                load[step_name] = load.get(step_name, 0) + 1
                task_id = task["id"]
                self.pool.submit(lambda tid=task_id: self.runner.execute(tid))
                progressed += 1
        return progressed

    def _requeue_due_failures(self) -> int:
        """Requeue FAILED tasks whose retry time has arrived. Returns the count."""
        count = 0
        for task in self.repo.list_due_failed():
            sm = self._state_machine_for(task)
            if sm is None:
                continue
            sm.requeue_failed(task["id"])
            count += 1
        return count

    def reclaim_stale(self, timeout_seconds: int) -> None:
        for task in self.repo.list_stale_running(timeout_seconds):
            sm = self._state_machine_for(task)
            if sm is None:
                continue
            sm.requeue_stale(task["id"])

    def _state_machine_for(self, task: dict[str, Any]) -> StateMachine | None:
        try:
            workflow = self.config.workflow(task["workflow"])
        except KeyError:
            return None
        return StateMachine(self.repo, self.log, workflow=workflow)
