"""Service functions behind the CLI: operate on AppContext, never echo directly
(so they stay easy to test)."""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Any

from runspool.app import AppContext
from runspool.models import EventType
from runspool.persistence.event_log import Event


class DuplicateTaskError(Exception):
    """An active task already exists for the same input."""

    def __init__(self, existing_id: int) -> None:
        super().__init__(f"active task already exists: {existing_id}")
        self.existing_id = existing_id


def _existing_path(input: str) -> bool:
    """Whether the input names an existing file or directory. An input that cannot
    be a path (plain text longer than the path limit, an embedded NUL) is not one:
    checking it raises ENAMETOOLONG or ValueError, which means "keep it as it is".
    Any other error (a permission problem on a real path) is raised as before."""
    try:
        return Path(input).expanduser().exists()
    except ValueError:
        return False
    except OSError as e:
        if e.errno == errno.ENAMETOOLONG:
            return False
        raise


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
    if _existing_path(input):
        # Store an existing file or directory absolutely, so steps find it whichever
        # directory the daemon or a later `run` starts in.
        input = str(Path(input).expanduser().resolve())
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


def approve_task(ctx: AppContext, task_id: int, *, by: str = "cli") -> None:
    _sm(ctx, task_id).approve(task_id, by=by)


def reject_task(ctx: AppContext, task_id: int, *, reason: str = "", by: str = "cli") -> None:
    _sm(ctx, task_id).reject(task_id, reason=reason, by=by)


# -- external steps: work items (runspool.engine.external) -----------------------------


class ItemUnavailable(ValueError):
    """A work item cannot be claimed, released or submitted in its current state."""


def _items(ctx: AppContext) -> Any:
    if ctx.items is None:
        raise ItemUnavailable("the active store has no work items (external steps)")
    return ctx.items


def get_item(ctx: AppContext, item_id: int) -> dict[str, Any]:
    item = _items(ctx).get(item_id)
    if item is None:
        raise KeyError(f"work item {item_id}")
    return item


def claim_item(ctx: AppContext, item_id: int, *, by: str) -> str:
    """Claim an open work item; returns the claim token needed to submit or release.
    Only one claimant wins: anyone else gets ItemUnavailable."""
    item = get_item(ctx, item_id)
    token = _items(ctx).claim(item_id, by=by)
    if token is None:
        status = (_items(ctx).get(item_id) or item)["status"]
        raise ItemUnavailable(f"work item {item_id} cannot be claimed: it is {status}")
    return token


def release_item(ctx: AppContext, item_id: int, *, token: str | None) -> None:
    """Give a claimed item back; ``token=None`` releases anyone's claim."""
    item = get_item(ctx, item_id)
    if not _items(ctx).release(item_id, token=token):
        why = "not claimed" if item["status"] != "claimed" else "claimed with another token"
        raise ItemUnavailable(f"work item {item_id} cannot be released: it is {why}")


def submit_item(
    ctx: AppContext, item_id: int, *, token: str, value: dict[str, Any], by: str | None = None
) -> None:
    """Submit a result for a claimed item. Only records it and queues the task: the
    step's validator runs in the executor (``run`` or the daemon)."""
    from runspool.persistence.work_items import ItemConflict

    if not isinstance(value, dict):
        raise ValueError("a submission must be a JSON object")
    item = get_item(ctx, item_id)
    if item["status"] != "claimed":
        raise ItemUnavailable(f"work item {item_id} cannot be submitted: it is {item['status']}")
    try:
        _sm(ctx, item["task_id"]).submit_external(item, token=token, value=value, by=by)
    except ItemConflict as exc:
        raise ItemUnavailable(str(exc)) from None


def _require_task(ctx: AppContext, task_id: int) -> dict:
    task = ctx.repo.get_task(task_id)
    if task is None:
        raise KeyError(task_id)
    return task


def _sm(ctx: AppContext, task_id: int):
    task = _require_task(ctx, task_id)
    return ctx.state_machine(task["workflow"])
