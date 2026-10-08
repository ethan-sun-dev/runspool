"""Human-readable text rendering: overview / task list / task detail / logs."""

from __future__ import annotations

import unicodedata
from collections.abc import Collection
from datetime import UTC, datetime
from typing import Any

from runspool.clock import to_local_text, utcnow_text
from runspool.models import TERMINAL_STATUSES, TaskStatus
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


_STATUS_GLYPH = {
    "ok": "✓",
    "failed": "✗",
    "running": "⟳",
    "deferred": "…",
    "degraded": "⚠",
    "interrupted": "↯",
}


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
        elif status in ("degraded", "deferred") and run.get("note"):
            out.append(f"        └ {run['note']}")
    return out


_LABEL_MAX = 50


def _char_width(ch: str) -> int:
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def truncate_by_width(text: str, max_width: int) -> str:
    """Cut ``text`` to ``max_width`` terminal columns, adding "..." when cut.

    CJK and other wide characters take two columns; a wide character that does not
    fit is dropped whole rather than split.
    """
    if sum(_char_width(ch) for ch in text) <= max_width:
        return text
    out: list[str] = []
    width = 0
    for ch in text:
        w = _char_width(ch)
        if width + w > max_width:
            break
        out.append(ch)
        width += w
    return "".join(out) + "..."


DEFAULT_LIST_LIMIT = 20


def select_tasks(
    tasks: list[dict[str, Any]],
    *,
    limit: int | None = None,
    statuses: Collection[str] = (),
    active: bool = False,
) -> list[dict[str, Any]]:
    """Newest first, optionally filtered by status / unfinished, then capped at ``limit``."""
    chosen = sorted(tasks, key=lambda t: t["id"], reverse=True)
    if statuses:
        chosen = [t for t in chosen if t["task_status"] in statuses]
    if active:
        chosen = [t for t in chosen if t["task_status"] not in TERMINAL_STATUSES]
    return chosen if limit is None else chosen[:limit]


def format_task_list(
    repo: TaskRepository,
    *,
    limit: int | None = DEFAULT_LIST_LIMIT,
    statuses: Collection[str] = (),
    active: bool = False,
) -> str:
    tasks = repo.list_all()
    if not tasks:
        return "(no tasks)"
    shown = select_tasks(tasks, limit=limit, statuses=statuses, active=active)
    rows: list[str] = []
    for t in shown:
        label = truncate_by_width(t.get("name") or t["input"], _LABEL_MAX)
        rows.append(
            f"#{t['id']:>4}  {t['task_status']:<16} {t['step']:<18} "
            f"{to_local_text(t['created_at'])}  {label}"
        )
    if not rows:
        rows.append("(no matching tasks)")
    matching = len(select_tasks(tasks, statuses=statuses, active=active))
    if len(shown) < matching:
        shown_ids = {t["id"] for t in shown}
        hidden_open = [
            t for t in tasks
            if t["id"] not in shown_ids and t["task_status"] not in TERMINAL_STATUSES
            and (not statuses or t["task_status"] in statuses)
        ]  # fmt: skip
        note = f"-- showing {len(shown)} of {matching}; -n N for more, --all for all"
        if hidden_open:
            ids = ", ".join(f"#{t['id']}" for t in sorted(hidden_open, key=lambda t: -t["id"])[:8])
            more = "" if len(hidden_open) <= 8 else ", ..."
            note += (
                f"\n-- {len(hidden_open)} unfinished not shown ({ids}{more}); --active lists them"
            )
        rows.append(note)
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
