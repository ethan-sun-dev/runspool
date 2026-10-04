"""Step-run history: writes to step_runs (per-step duration / outcome / error)."""

from __future__ import annotations

from typing import Any

from runspool.persistence.connection import Database


class StepRunLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def start(self, task_id: int, step: str) -> int:
        with self.db.connect() as conn:
            cur = conn.execute(
                "insert into step_runs (task_id, step, status) values (?,?,?)",
                (task_id, step, "running"),
            )
            return int(cur.lastrowid)

    def finish(
        self,
        run_id: int,
        *,
        status: str,
        duration_ms: int,
        error: str | None = None,
        note: str | None = None,
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "update step_runs set status = ?, finished_at = datetime('now'), "
                "duration_ms = ?, error = ?, note = ? where id = ?",
                (status, duration_ms, error, note, run_id),
            )

    def count_runs(self, task_id: int, step: str) -> int:
        """How many times this task has run ``step`` so far (any outcome)."""
        with self.db.connect() as conn:
            row = conn.execute(
                "select count(*) from step_runs where task_id = ? and step = ?", (task_id, step)
            ).fetchone()
            return int(row[0])

    def has_degraded(self, task_id: int) -> bool:
        """Whether the latest run of any step of this task was ``degraded``.

        Only the latest run per step counts: a later successful re-run of a step
        clears an earlier degraded run of it.
        """
        latest: dict[str, str] = {}
        for run in self.list_for_task(task_id):  # ascending id: later runs overwrite
            latest[run["step"]] = run["status"]
        return any(status == "degraded" for status in latest.values())

    def close_running_for_task(self, task_id: int, *, status: str = "interrupted") -> int:
        """Close any still-"running" step_run rows for a task.

        Used on recovery/reclaim: the worker that owned the row is gone, so the
        row would otherwise hang in "running" forever and a re-execution would
        insert a second row. Returns the number of rows closed.
        """
        with self.db.connect() as conn:
            cur = conn.execute(
                "update step_runs set status = ?, finished_at = datetime('now') "
                "where task_id = ? and status = 'running'",
                (status, task_id),
            )
            return cur.rowcount

    def list_for_task(self, task_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "select * from step_runs where task_id = ? order by id asc", (task_id,)
            ).fetchall()
            return [dict(r) for r in rows]
