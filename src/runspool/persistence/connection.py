"""SQLite connection wrapper."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from runspool.persistence.schema import migrate


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def init(self) -> int:
        """Create or upgrade the schema; returns the schema version.

        The whole upgrade is one ``BEGIN IMMEDIATE`` transaction: concurrent openers
        (a daemon and a CLI) take turns, and each re-reads the version inside it, so
        a migration runs once and an interrupted one leaves nothing half-applied.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        try:
            _configure(conn)
            conn.execute("begin immediate")
            try:
                version = migrate(conn)
            except BaseException:
                conn.execute("rollback")
                raise
            conn.execute("commit")
            return version
        finally:
            conn.close()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        _configure(conn)
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def _configure(conn: sqlite3.Connection) -> None:
    conn.execute("pragma foreign_keys = on")
    # busy_timeout first: switching to WAL itself needs a lock, and without the
    # timeout a second opener fails at once with "database is locked". WAL lets
    # readers and a single writer proceed concurrently once it is on.
    conn.execute("pragma busy_timeout = 5000")
    conn.execute("pragma journal_mode = wal")
