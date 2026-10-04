"""``tasks``: the task lifecycle. The state machine is the only writer of task status.

This service is core and not replaceable. It hands out state machines bound to a
workflow and the public operations on tasks (add, pause, resume, ...), so plugins
(e.g. a sub-flow command) never write task rows themselves.
"""

from __future__ import annotations

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
        # The command functions take an application context. (Imported here: the
        # app module boots the kernel, which loads this plugin.)
        from runspool.app import AppContext

        self._app = AppContext(
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

    def for_context(self, ctx: Any) -> TasksService | _BoundTasks:
        if ctx.fiber.parent is None:
            return self  # the application itself (CLI, embedding code), not a plugin
        return _BoundTasks(self, ctx)

    def approve(self, task_id: int, *, by: str) -> None:
        """Approve the step a task is waiting on, for that one attempt."""
        commands.approve_task(self._app, task_id, by=by)

    def reject(self, task_id: int, *, by: str, reason: str = "") -> None:
        commands.reject_task(self._app, task_id, reason=reason, by=by)

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


class _BoundTasks:
    """The tasks service as one plugin sees it: approvals and rejections it makes are
    recorded under its own name (``plugin:<entry>``), whatever ``by`` it passes."""

    def __init__(self, service: TasksService, ctx: Any) -> None:
        self._service = service
        self._who = f"plugin:{ctx.fiber.name}"

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)

    def approve(self, task_id: int, *, by: str = "") -> None:
        self._service.approve(task_id, by=self._who + (f" ({by})" if by else ""))

    def reject(self, task_id: int, *, by: str = "", reason: str = "") -> None:
        self._service.reject(task_id, by=self._who + (f" ({by})" if by else ""), reason=reason)


plugin = Plugin(
    name="tasks",
    apply=lambda ctx, config: ctx.provide("tasks", TasksService(ctx.config, ctx.store)),
    inject=["config", "store"],
)
