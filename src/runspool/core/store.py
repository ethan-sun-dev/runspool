"""``store``: the persistence seam — tasks, events and step runs. Default: SQLite.

The store exposes primitives only (create, atomic claim, allow-listed field
updates, event and step-run logs). Status transitions are decided by the state
machine, never by the store. Any replacement implementation must pass
``runspool.testing.store_contract``.
"""

from __future__ import annotations

from runspool.kernel import Plugin
from runspool.persistence.connection import Database
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository
from runspool.persistence.step_run_log import StepRunLog


class Store:
    """SQLite store. ``repo``, ``log`` and ``step_runs`` are the contract surface;
    ``db`` is SQLite-specific and only for code that knows it has this store."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.repo = TaskRepository(db)
        self.log = EventLog(db)
        self.step_runs = StepRunLog(db)

    def check(self) -> str:
        """Raise if the store is unusable; otherwise describe where it lives."""
        self.db.init()
        with self.db.connect() as conn:
            conn.execute("select 1").fetchone()
        return str(self.db.path)


def _apply(ctx, config) -> None:
    db = Database(ctx.config.database_path)
    db.init()
    store = Store(db)
    if ctx.config.first_task_id is not None:
        store.repo.ensure_next_id(ctx.config.first_task_id)
    ctx.provide("store", store)


plugin = Plugin(name="store-sqlite", apply=_apply, inject=["config"])
