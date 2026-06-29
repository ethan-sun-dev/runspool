"""JSON-shaped views over tasks, events, runs, and the inspect summary.

These builders return plain dicts/lists ready for ``json.dumps``. They are the
contract for ``--json`` output: stable field names that humans, shell scripts,
and AI agents can all rely on.
"""

from __future__ import annotations

from typing import Any

from runspool.app import AppContext
from runspool.builtin_steps.workspace import list_artifacts
from runspool.models import TaskStatus

# Task fields exposed in JSON output, in a stable order.
_TASK_FIELDS = (
    "id",
    "name",
    "input",
    "workflow",
    "step",
    "task_status",
    "priority",
    "retry_count",
    "max_retries",
    "progress",
    "last_error",
    "created_at",
    "updated_at",
)

# Actions a caller can meaningfully take next, keyed by current status.
_ACTIONS: dict[str, list[str]] = {
    TaskStatus.QUEUED: ["pause", "terminate", "set-priority"],
    TaskStatus.RUNNING: ["pause", "terminate"],
    TaskStatus.PAUSE_PENDING: ["terminate"],
    TaskStatus.PAUSED: ["resume", "terminate"],
    TaskStatus.FAILED: ["retry", "set-step", "terminate"],
    TaskStatus.MANUAL_REQUIRED: ["retry", "set-step", "set-retries", "terminate"],
    TaskStatus.COMPLETED: [],
    TaskStatus.TERMINATED: [],
}


def task_view(task: dict[str, Any]) -> dict[str, Any]:
    return {k: task.get(k) for k in _TASK_FIELDS}


def list_view(tasks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [task_view(t) for t in tasks]


def overview_view(ctx: AppContext) -> dict[str, int]:
    return {
        status.value: len(ctx.repo.list_by_status(status))
        for status in TaskStatus
        if ctx.repo.list_by_status(status)
    }


def events_view(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "event_type": e["event_type"],
            "step": e["step"],
            "message": e["message"],
            "created_at": e["created_at"],
        }
        for e in events
    ]


def runs_view(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "step": r["step"],
            "status": r["status"],
            "duration_ms": r["duration_ms"],
            "error": r["error"],
            "started_at": r["started_at"],
            "finished_at": r["finished_at"],
        }
        for r in runs
    ]


def detail_view(ctx: AppContext, task: dict[str, Any]) -> dict[str, Any]:
    view = task_view(task)
    view["events"] = events_view(ctx.log.list_for_task(task["id"], limit=20))
    view["step_runs"] = runs_view(ctx.step_runs.list_for_task(task["id"]))
    return view


def _suggested_next_action(task: dict[str, Any]) -> str:
    status = task["task_status"]
    tid = task["id"]
    if status == TaskStatus.QUEUED:
        return f"Advance with `runspool run`, or start the daemon. (task {tid})"
    if status == TaskStatus.RUNNING:
        return f"Task is running; wait, or pause with `runspool pause {tid}`."
    if status == TaskStatus.PAUSE_PENDING:
        return "Pause was requested; it applies after the current step finishes."
    if status == TaskStatus.PAUSED:
        return f"Resume with `runspool resume {tid}`."
    if status == TaskStatus.FAILED:
        return f"A retry is pending; force it now with `runspool retry {tid}`."
    if status == TaskStatus.MANUAL_REQUIRED:
        err = task.get("last_error") or "the step failed repeatedly"
        return f"{err}. Resolve the cause, then run `runspool retry {tid}`."
    if status == TaskStatus.COMPLETED:
        return "Task is complete; no action needed."
    if status == TaskStatus.TERMINATED:
        return "Task was terminated; create a new task to redo the work."
    return "No suggested action."


def inspect_view(ctx: AppContext, task: dict[str, Any]) -> dict[str, Any]:
    """Agent-friendly snapshot: enough state and guidance to decide what to do."""
    status = task["task_status"]
    return {
        "id": task["id"],
        "name": task.get("name"),
        "input": task["input"],
        "status": status,
        "workflow": task["workflow"],
        "current_step": task["step"],
        "priority": task["priority"],
        "retry_count": task["retry_count"],
        "max_retries": task["max_retries"],
        "progress": task.get("progress"),
        "last_error": task.get("last_error"),
        "recent_events": events_view(ctx.log.list_for_task(task["id"], limit=5)),
        "step_runs": runs_view(ctx.step_runs.list_for_task(task["id"])),
        "artifacts": list_artifacts(ctx.config, task),
        "available_actions": _ACTIONS.get(status, []),
        "suggested_next_action": _suggested_next_action(task),
    }
