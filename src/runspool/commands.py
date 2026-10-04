"""Service functions behind the CLI: operate on AppContext, never echo directly
(so they stay easy to test)."""

from __future__ import annotations

from typing import Any

from runspool.app import AppContext
from runspool.models import EventType
from runspool.persistence.event_log import Event


class DuplicateTaskError(Exception):
    """An active task already exists for the same input."""

    def __init__(self, existing_id: int) -> None:
        super().__init__(f"active task already exists: {existing_id}")
        self.existing_id = existing_id


def add_task(
    ctx: AppContext,
    input: str,
    *,
    workflow: str,
    force: bool = False,
    name: str | None = None,
    metadata: dict[str, Any] | None = None,
    parent_task_id: int | None = None,
) -> int:
    wf = ctx.config.workflow(workflow)  # unknown workflow raises KeyError
    if parent_task_id is not None and ctx.repo.get_task(parent_task_id) is None:
        raise KeyError(parent_task_id)
    if not force:
        existing = ctx.repo.find_active_by_input(input)
        if existing is not None:
            raise DuplicateTaskError(existing["id"])
    message = "task created" if parent_task_id is None else f"task created from {parent_task_id}"
    # The task, its metadata and its "created" event are written in one transaction:
    # nothing can claim the task before what it needs to run is recorded with it.
    return ctx.repo.create_task(
        input=input,
        workflow=workflow,
        first_step=wf.first_step(),
        max_retries=ctx.config.scheduler.max_retries,
        name=name,
        metadata=metadata,
        parent_task_id=parent_task_id,
        event=Event(EventType.CREATED, step=wf.first_step(), message=message),
    )


def pause_task(ctx: AppContext, task_id: int) -> None:
    _sm(ctx, task_id).request_pause(task_id)


def resume_task(ctx: AppContext, task_id: int) -> None:
    _sm(ctx, task_id).resume(task_id)


def terminate_task(ctx: AppContext, task_id: int) -> None:
    _sm(ctx, task_id).request_terminate(task_id)


def retry_task(ctx: AppContext, task_id: int) -> None:
    _sm(ctx, task_id).retry(task_id)


def set_priority(ctx: AppContext, task_id: int, priority: int) -> None:
    _require_task(ctx, task_id)
    ctx.repo.update_fields(task_id, {"priority": priority})


def set_retries(ctx: AppContext, task_id: int, max_retries: int) -> None:
    if max_retries < 0:
        raise ValueError(f"max-retries must be >= 0, got {max_retries}")
    _sm(ctx, task_id).set_retries(task_id, max_retries)


def set_step(ctx: AppContext, task_id: int, step: str, *, force: bool = False) -> None:
    _sm(ctx, task_id).set_step(task_id, step, force=force)


def wake_task(ctx: AppContext, task_id: int) -> None:
    _sm(ctx, task_id).wake(task_id)


def _require_task(ctx: AppContext, task_id: int) -> dict:
    task = ctx.repo.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    return task


def _sm(ctx: AppContext, task_id: int):
    task = _require_task(ctx, task_id)
    return ctx.state_machine(task["workflow"])
