"""Task event log."""

from __future__ import annotations

import json
from typing import Any

from runspool.models import EventType
from runspool.persistence.connection import Database


class EventLog:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(
        self,
        task_id: int,
        event_type: EventType,
        *,
        step: str | None = None,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        payload_json = json.dumps(payload, ensure_ascii=False) if payload is not None else None
        with self.db.connect() as conn:
            conn.execute(
                "insert into task_events (task_id, event_type, step, message, payload_json) "
                "values (?,?,?,?,?)",
                (task_id, event_type, step, message, payload_json),
            )

    def list_for_task(self, task_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "select * from task_events where task_id = ? order by id desc"
        params: list[Any] = [task_id]
        if limit is not None:
            sql += " limit ?"
            params.append(limit)
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
