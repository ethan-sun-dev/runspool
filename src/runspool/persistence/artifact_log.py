"""Recorded artifacts: what steps reported producing (type, path, sha256).

Rows are written when a step returns ``StepResult.artifacts`` and are never
updated. A step that runs again records again, so a task can hold several rows of
one type; readers usually want the latest. Rows from a run that did not finish
cleanly (the process died, the run was interrupted) are skipped by default: only
runs whose status is ``ok`` or ``degraded`` count.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from runspool.engine.artifacts import RecordedArtifact
from runspool.persistence.connection import Database

_FINISHED = ("ok", "degraded")


class ArtifactLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def record(
        self,
        task_id: int,
        step: str,
        artifacts: Sequence[RecordedArtifact],
        *,
        step_run_id: int | None = None,
    ) -> None:
        """Store ``artifacts`` in one transaction (all of them or none)."""
        if not artifacts:
            return
        with self.db.connect() as conn:
            conn.executemany(
                "insert into artifacts (task_id, step, step_run_id, type, path, sha256, "
                "size_bytes) values (?,?,?,?,?,?,?)",
                [
                    (task_id, step, step_run_id, a.type, a.path, a.sha256, a.size_bytes)
                    for a in artifacts
                ],
            )

    def list_for_task(
        self, task_id: int, *, type: str | None = None, include_unfinished: bool = False
    ) -> list[dict[str, Any]]:
        """Rows for a task, oldest first; optionally only one ``type``."""
        sql = (
            "select a.* from artifacts a left join step_runs r on r.id = a.step_run_id "
            "where a.task_id = ?"
        )
        params: list[Any] = [task_id]
        if type is not None:
            sql += " and a.type = ?"
            params.append(type)
        if not include_unfinished:
            sql += f" and (a.step_run_id is null or r.status in ({','.join('?' * len(_FINISHED))}))"
            params.extend(_FINISHED)
        sql += " order by a.id asc"
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]

    def latest(self, task_id: int) -> list[dict[str, Any]]:
        """The most recent row of each type for a task, in the order they were recorded."""
        latest: dict[str, dict[str, Any]] = {}
        for row in self.list_for_task(task_id):
            latest.pop(row["type"], None)
            latest[row["type"]] = row
        return list(latest.values())
