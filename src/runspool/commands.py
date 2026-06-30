"""Service functions behind the CLI: operate on AppContext, never echo directly
(so they stay easy to test)."""

from __future__ import annotations

from runspool.app import AppContext
from runspool.models import EventType, TaskStatus
from runspool.persistence.state_machine import IllegalTransition


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
) -> int:
    wf = ctx.config.workflow(workflow)  # unknown workflow raises KeyError
    if not force:
        existing = ctx.repo.find_active_by_input(input)
        if existing is not None:
            raise DuplicateTaskError(existing["id"])
    task_id = ctx.repo.create_task(
        input=input,
        workflow=workflow,
        first_step=wf.first_step(),
        max_retries=ctx.config.scheduler.max_retries,
        name=name,
    )
    ctx.log.add(task_id, EventType.CREATED, step=wf.first_step(), message="task created")
    return task_id


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
    # Mirror the config model's ge=0 constraint: a negative cap would make
    # fail()'s `retry_count > max_retries` check route the very first failure
    # straight to manual_required, silently disabling retries.
    if max_retries < 0:
        raise ValueError(f"max-retries must be >= 0, got {max_retries}")
    _require_task(ctx, task_id)
    ctx.repo.update_fields(task_id, {"max_retries": max_retries, "retry_count": 0})


_SET_STEP_ALLOWED = (TaskStatus.FAILED, TaskStatus.MANUAL_REQUIRED)


def set_step(ctx: AppContext, task_id: int, step: str, *, force: bool = False) -> None:
    task = _require_task(ctx, task_id)
    wf = ctx.config.workflow(task["workflow"])
    if step not in wf.steps:
        raise ValueError(f"step {step!r} is not part of workflow {wf.name!r}")
    status = task["task_status"]
    # Moving a running/queued task mid-flight, or rewinding a finished one, is a
    # foot-gun; restrict to recovery states unless explicitly forced.
    if not force and status not in _SET_STEP_ALLOWED:
        raise IllegalTransition(
            task_id, status, "set-step", allowed="failed or manual_required (use --force)"
        )
    ctx.repo.update_fields(task_id, {"step": step})


def _require_task(ctx: AppContext, task_id: int) -> dict:
    task = ctx.repo.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    return task


def _sm(ctx: AppContext, task_id: int):
    task = _require_task(ctx, task_id)
    return ctx.state_machine(task["workflow"])
