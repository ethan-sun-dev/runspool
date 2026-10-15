"""SQLite connection wrapper."""

from __future__ import annotations

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from runspool.executor_lock import ExecutorLock
from runspool.persistence.schema import SCHEMA_VERSION, migrate, stored_version


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def init(self, *, upgrade_lock: Path | str | None = None) -> int:
        """Create or upgrade the schema; returns the schema version.

        The whole upgrade is one ``BEGIN IMMEDIATE`` transaction: concurrent openers
        (a daemon and a CLI) take turns, and each re-reads the version inside it, so
        a migration runs once and an interrupted one leaves nothing half-applied.

        With ``upgrade_lock`` (the executor lock file), an existing database is only
        upgraded while holding that lock: if another process is executing tasks, this
        raises :class:`~runspool.executor_lock.ExecutorBusy` instead of changing the
        schema under it. Opening an up-to-date database never takes the lock.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, isolation_level=None)
        lock = None
        try:
            _configure(conn)
            if upgrade_lock is not None:
                current = stored_version(conn)
                if 0 < current < SCHEMA_VERSION:
                    lock = ExecutorLock(upgrade_lock)
                    # This process may already hold it (an embedded executor that
                    # opens the database again): then it is ours to upgrade.
                    if not lock.acquire() and lock.holder() != os.getpid():
                        raise lock.busy_error(
                            f"cannot upgrade the database schema (v{current} -> "
                            f"v{SCHEMA_VERSION}) now"
                        )
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
            if lock is not None:
                lock.release()

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
    # Switching an older (rollback-journal) database to WAL needs an exclusive lock and
    # does not wait on busy_timeout: retry briefly while another opener holds it.
    for attempt in range(50):
        try:
            conn.execute("pragma journal_mode = wal")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or attempt == 49:
                raise
            time.sleep(0.1)
