"""Task event log."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Any

from runspool.models import EventType
from runspool.persistence.connection import Database


@dataclass(frozen=True)
class Event:
    """An event to record, e.g. together with a transition in the same transaction."""

    type: EventType
    step: str | None = None
    message: str | None = None
    payload: dict[str, Any] | None = None


def insert_event(conn: sqlite3.Connection, task_id: int, event: Event) -> None:
    payload_json = (
        json.dumps(event.payload, ensure_ascii=False) if event.payload is not None else None
    )
    conn.execute(
        "insert into task_events (task_id, event_type, step, message, payload_json) "
        "values (?,?,?,?,?)",
        (task_id, event.type, event.step, event.message, payload_json),
    )


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
        with self.db.connect() as conn:
            insert_event(conn, task_id, Event(event_type, step, message, payload))

    def list_for_task(self, task_id: int, limit: int | None = None) -> list[dict[str, Any]]:
        sql = "select * from task_events where task_id = ? order by id desc"
        params: list[Any] = [task_id]
        if limit is not None:
            sql += " limit ?"
            params.append(limit)
        with self.db.connect() as conn:
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
