"""SQLite connection wrapper."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from runspool.persistence.schema import SCHEMA


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            # Idempotent migrations: ``create table if not exists`` does not add
            # columns to an existing table, so backfill optional columns here.
            cols = [r[1] for r in conn.execute("pragma table_info(tasks)").fetchall()]
            if "progress" not in cols:
                conn.execute("alter table tasks add column progress text")
            if "name" not in cols:
                conn.execute("alter table tasks add column name text")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("pragma foreign_keys = on")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
