"""Human-readable text rendering: overview / task list / task detail / logs."""

from __future__ import annotations

from datetime import UTC, datetime

from runspool.clock import to_local_text, utcnow_text
from runspool.models import TaskStatus
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository

_FMT = "%Y-%m-%d %H:%M:%S"


def humanize_age(ts: str | None, *, now_text: str | None = None) -> str:
    """Render a UTC timestamp as a relative age (e.g. ``3m ago``)."""
    if not ts:
        return ""
    now = datetime.strptime(now_text or utcnow_text(), _FMT).replace(tzinfo=UTC)
    then = datetime.strptime(ts, _FMT).replace(tzinfo=UTC)
    sec = max(int((now - then).total_seconds()), 0)
    if sec < 60:
        return f"{sec}s ago"
    if sec < 3600:
        return f"{sec // 60}m ago"
    return f"{sec // 3600}h ago"


def humanize_duration_ms(ms: int | None) -> str:
    """Render a millisecond duration as ``55s`` / ``9m35s`` / ``1h02m``."""
    if not ms or ms <= 0:
        return ""
    total = int(ms // 1000)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        return f"{total // 60}m{total % 60:02d}s"
    return f"{total // 3600}h{(total % 3600) // 60:02d}m"


def format_overview(repo: TaskRepository) -> str:
    lines = ["Tasks by status:"]
    for status in TaskStatus:
        count = len(repo.list_by_status(status))
        if count:
            lines.append(f"  {status}: {count}")
    if len(lines) == 1:
        lines.append("  (no tasks yet)")
    return "\n".join(lines)


_STATUS_GLYPH = {"ok": "✓", "failed": "✗", "running": "⟳", "deferred": "…"}


def format_task_detail(
    repo: TaskRepository,
    log: EventLog,
    task_id: int,
    *,
    runs: list[dict] | None = None,
    ordered_steps: tuple[str, ...] | None = None,
) -> str:
    task = repo.get_task(task_id)
    if task is None:
        return f"task {task_id} not found"
    lines = [
        f"Task #{task['id']}",
        f"  Input:    {task['input']}",
        f"  Name:     {task.get('name') or '(unnamed)'}",
        f"  Workflow: {task['workflow']}",
        f"  Step:     {task['step']}",
        f"  Status:   {task['task_status']}",
        f"  Priority: {task['priority']}   Retries: {task['retry_count']}/{task['max_retries']}",
    ]
    if task["task_status"] == TaskStatus.RUNNING and task.get("progress"):
        age = humanize_age(task.get("heartbeat_at"))
        suffix = f" ({age})" if age else ""
        lines.append(f"  Progress: {task['progress']}{suffix}")
    if task["last_error"]:
        lines.append(f"  Last error: {task['last_error']}")
    events = log.list_for_task(task_id, limit=5)
    if events:
        lines.append("  Recent events:")
        for e in events:
            lines.append(
                f"    [{to_local_text(e['created_at'])}] {e['event_type']} "
                f"{e['message'] or ''}".rstrip()
            )
    if runs is not None:
        lines.extend(_render_step_timeline(runs, ordered_steps, current_step=task["step"]))
    return "\n".join(lines)


def _render_step_timeline(
    runs: list[dict],
    ordered_steps: tuple[str, ...] | None,
    *,
    current_step: str,
) -> list[str]:
    """Render a step timeline in workflow order; fall back to run order."""
    last_run: dict[str, dict] = {}
    for r in runs:  # runs are id-ascending, so later entries win per step
        last_run[r["step"]] = r
    sequence = ordered_steps if ordered_steps else tuple(r["step"] for r in runs)
    if not sequence:
        return []
    out = ["  Step timeline:"]
    for step in sequence:
        run = last_run.get(step)
        if run is None:
            glyph = "▶" if step == current_step else "○"
            out.append(f"    {glyph} {step}")
            continue
        status = run.get("status")
        glyph = _STATUS_GLYPH.get(status, "•")
        if status == "running":
            out.append(f"    {glyph} {step:<18} running")
        else:
            dur = humanize_duration_ms(run.get("duration_ms"))
            out.append(f"    {glyph} {step:<18} {dur}".rstrip())
        if status == "failed" and run.get("error"):
            out.append(f"        └ {run['error']}")
    return out


_LABEL_MAX = 50


def format_task_list(repo: TaskRepository) -> str:
    tasks = repo.list_all()
    tasks.sort(key=lambda t: t["id"], reverse=True)
    if not tasks:
        return "(no tasks)"
    rows: list[str] = []
    for t in tasks:
        label = t.get("name") or t["input"]
        if len(label) > _LABEL_MAX:
            label = label[:_LABEL_MAX] + "..."
        rows.append(
            f"#{t['id']:>4}  {t['task_status']:<16} {t['step']:<18} "
            f"{to_local_text(t['created_at'])}  {label}"
        )
    return "\n".join(rows)


def format_logs(log: EventLog, task_id: int, *, limit: int) -> str:
    events = log.list_for_task(task_id, limit=limit)
    if not events:
        return f"task {task_id} has no events"
    return "\n".join(
        f"[{to_local_text(e['created_at'])}] {e['event_type']} "
        f"{e['step'] or ''} {e['message'] or ''}".rstrip()
        for e in events
    )
