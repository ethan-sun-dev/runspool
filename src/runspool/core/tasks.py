"""``tasks``: the task lifecycle. The state machine is the only writer of task status.

This service is core and not replaceable. It hands out state machines bound to a
workflow and the public operations on tasks (add, pause, resume, ...), so plugins
(e.g. a sub-flow command) never write task rows themselves.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from runspool import commands
from runspool.config import AppConfig
from runspool.core.store import Store
from runspool.kernel import Plugin
from runspool.persistence.state_machine import StateMachine


class TasksService:
    def __init__(self, config: AppConfig, store: Store) -> None:
        self._config = config
        self._store = store
        # The command functions take an application context; give them exactly the
        # pieces they use.
        self._app = SimpleNamespace(
            config=config, db=store.db, repo=store.repo, log=store.log, step_runs=store.step_runs
        )

    def machine(self, workflow: str) -> StateMachine:
        return StateMachine(
            self._store.repo,
            self._store.log,
            workflow=self._config.workflow(workflow),
            step_runs=self._store.step_runs,
        )

    def get(self, task_id: int) -> dict[str, Any] | None:
        return self._store.repo.get_task(task_id)

    def add(
        self,
        input: str,
        *,
        workflow: str,
        name: str | None = None,
        force: bool = False,
        metadata: dict[str, Any] | None = None,
        parent: int | None = None,
    ) -> int:
        """Create a task, e.g. a sub-flow of ``parent`` carrying what it needs in
        ``metadata``. Task, metadata and its "created" event commit together."""
        return commands.add_task(
            self._app,
            input,
            workflow=workflow,
            name=name,
            force=force,
            metadata=metadata,
            parent_task_id=parent,
        )

    def children(self, task_id: int) -> list[dict[str, Any]]:
        return self._store.repo.list_children(task_id)

    def pause(self, task_id: int) -> None:
        commands.pause_task(self._app, task_id)

    def resume(self, task_id: int) -> None:
        commands.resume_task(self._app, task_id)

    def terminate(self, task_id: int) -> None:
        commands.terminate_task(self._app, task_id)

    def retry(self, task_id: int) -> None:
        commands.retry_task(self._app, task_id)


plugin = Plugin(
    name="tasks",
    apply=lambda ctx, config: ctx.provide("tasks", TasksService(ctx.config, ctx.store)),
    inject=["config", "store"],
)
