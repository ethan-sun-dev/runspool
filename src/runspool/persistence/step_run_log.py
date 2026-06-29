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
        self, run_id: int, *, status: str, duration_ms: int, error: str | None = None
    ) -> None:
        with self.db.connect() as conn:
            conn.execute(
                "update step_runs set status = ?, finished_at = datetime('now'), "
                "duration_ms = ?, error = ? where id = ?",
                (status, duration_ms, error, run_id),
            )

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
