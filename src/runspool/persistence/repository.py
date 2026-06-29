"""Task repository: pure CRUD, with no state-transition rules."""

from __future__ import annotations

import sqlite3
from typing import Any

from runspool.models import TaskStatus
from runspool.persistence.connection import Database

# Columns that may be written through update_fields (prevents arbitrary-column
# injection). ``id`` and ``input`` are intentionally excluded: they are the
# task's immutable identity and must never be rewritten after creation.
UPDATABLE_COLUMNS = frozenset(
    {
        "name",
        "workflow",
        "step",
        "task_status",
        "priority",
        "retry_count",
        "max_retries",
        "locked_by",
        "locked_at",
        "heartbeat_at",
        "pause_requested",
        "terminate_requested",
        "last_error",
        "next_retry_at",
        "progress",
    }
)


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def create_task(
        self,
        *,
        input: str,
        workflow: str,
        first_step: str,
        max_retries: int,
        name: str | None = None,
    ) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "insert into tasks (input, name, workflow, step, task_status, max_retries) "
                "values (?,?,?,?,?,?)",
                (input, name, workflow, first_step, TaskStatus.QUEUED, max_retries),
            )
            return int(cur.lastrowid)

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("select * from tasks where id = ?", (task_id,)).fetchone()
            return _row_to_dict(row)

    def list_by_status(self, status: TaskStatus) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "select * from tasks where task_status = ? "
                "order by priority desc, updated_at asc, created_at asc, id asc",
                (status,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def list_all(self) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute("select * from tasks order by id asc").fetchall()
            return [_row_to_dict(r) for r in rows]

    def list_due_failed(self) -> list[dict[str, Any]]:
        """FAILED tasks whose scheduled retry time has arrived (or is unset)."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "select * from tasks where task_status = ? "
                "and (next_retry_at is null or next_retry_at <= datetime('now')) "
                "order by priority desc, updated_at asc, id asc",
                (TaskStatus.FAILED,),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def list_stale_running(self, timeout_seconds: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "select * from tasks where task_status = ? "
                "and (heartbeat_at is null or heartbeat_at < datetime('now', ?))",
                (TaskStatus.RUNNING, f"-{timeout_seconds} seconds"),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def find_active_by_input(self, input: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "select * from tasks where input = ? "
                "and task_status not in ('completed', 'terminated') "
                "order by id desc limit 1",
                (input,),
            ).fetchone()
            return _row_to_dict(row)

    def update_fields(self, task_id: int, fields: dict[str, Any]) -> None:
        # Empty fields is a no-op; a missing task_id affects 0 rows by standard
        # SQL semantics (no error). Existence checks belong to the StateMachine;
        # the repository does pure CRUD only.
        if not fields:
            return
        bad = set(fields) - UPDATABLE_COLUMNS
        if bad:
            raise ValueError(f"columns not updatable: {sorted(bad)}")
        assignments = ", ".join(f"{col} = ?" for col in fields)
        values = list(fields.values())
        values.append(task_id)
        with self.db.connect() as conn:
            conn.execute(
                f"update tasks set {assignments}, updated_at = datetime('now') where id = ?",
                values,
            )


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None
