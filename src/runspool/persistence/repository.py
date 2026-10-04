"""Task repository: pure CRUD, with no state-transition rules."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from runspool.models import TERMINAL_STATUSES, TaskStatus
from runspool.persistence.connection import Database
from runspool.persistence.event_log import Event, insert_event

# Columns anyone may write with update_fields: a step's StepResult.updates, admin
# commands, plugins. ``id`` and ``input`` are the task's immutable identity.
UPDATABLE_COLUMNS = frozenset({"name", "priority", "max_retries", "progress", "metadata"})

# Lifecycle columns. Only the state machine writes these, through transition();
# claim_queued and heartbeat cover the two hot paths. Keeping them out of
# update_fields is what makes "status is decided by the state machine" enforceable.
LIFECYCLE_COLUMNS = frozenset(
    {
        "workflow",
        "step",
        "task_status",
        "retry_count",
        "locked_by",
        "locked_at",
        "heartbeat_at",
        "pause_requested",
        "terminate_requested",
        "last_error",
        "next_retry_at",
        "claim_token",
        "approval_grant",
    }
)

_UNSET = object()


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
        metadata: dict[str, Any] | None = None,
        parent_task_id: int | None = None,
        event: Event | None = None,
    ) -> int:
        """Insert a QUEUED task (and ``event`` for it, in the same transaction)."""
        with self.db.connect() as conn:
            cur = conn.execute(
                "insert into tasks (input, name, workflow, step, task_status, max_retries, "
                "metadata, parent_task_id) values (?,?,?,?,?,?,?,?)",
                (
                    input,
                    name,
                    workflow,
                    first_step,
                    TaskStatus.QUEUED,
                    max_retries,
                    _dump(metadata),
                    parent_task_id,
                ),
            )
            task_id = int(cur.lastrowid)
            if event is not None:
                insert_event(conn, task_id, event)
            return task_id

    def list_children(self, parent_task_id: int) -> list[dict[str, Any]]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "select * from tasks where parent_task_id = ? order by id asc", (parent_task_id,)
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def ensure_next_id(self, at_least: int) -> int:
        """Make the next task id at least ``at_least`` (never lowers it).

        For a new database that continues an older system's numbering. Returns the
        id the next task will get.
        """
        with self.db.connect() as conn:
            row = conn.execute("select seq from sqlite_sequence where name = 'tasks'").fetchone()
            seq = int(row[0]) if row else 0
            highest = int(conn.execute("select coalesce(max(id), 0) from tasks").fetchone()[0])
            current = max(seq, highest)
            if at_least - 1 > current:
                if row:
                    conn.execute(
                        "update sqlite_sequence set seq = ? where name = 'tasks'", (at_least - 1,)
                    )
                else:
                    conn.execute(
                        "insert into sqlite_sequence (name, seq) values ('tasks', ?)",
                        (at_least - 1,),
                    )
                current = at_least - 1
            return current + 1

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
        """Executing tasks (RUNNING or PAUSE_PENDING) whose heartbeat is too old."""
        with self.db.connect() as conn:
            rows = conn.execute(
                "select * from tasks where task_status in (?, ?) "
                "and (heartbeat_at is null or heartbeat_at < datetime('now', ?))",
                (TaskStatus.RUNNING, TaskStatus.PAUSE_PENDING, f"-{timeout_seconds} seconds"),
            ).fetchall()
            return [_row_to_dict(r) for r in rows]

    def find_active_by_input(self, input: str) -> dict[str, Any] | None:
        terminal = tuple(TERMINAL_STATUSES)
        marks = ",".join("?" * len(terminal))
        with self.db.connect() as conn:
            row = conn.execute(
                f"select * from tasks where input = ? and task_status not in ({marks}) "
                "order by id desc limit 1",
                (input, *terminal),
            ).fetchone()
            return _row_to_dict(row)

    def claim_queued(
        self,
        task_id: int,
        *,
        worker: str,
        now: str,
        token: str | None = None,
        event: Event | None = None,
    ) -> bool:
        """Atomically claim a task only if it is still QUEUED.

        The conditional UPDATE + rowcount check makes claiming safe even when a
        separate process (e.g. the daemon and a CLI) races for the same task:
        exactly one UPDATE matches the ``task_status='queued'`` predicate. The claim
        records the claim ``token``, clears any pending ``next_retry_at``, and writes
        ``event`` in the same transaction. Returns True if this caller won the claim.
        """
        with self.db.connect() as conn:
            cur = conn.execute(
                "update tasks set task_status = ?, locked_by = ?, locked_at = ?, "
                "heartbeat_at = ?, claim_token = ?, next_retry_at = null, "
                "updated_at = datetime('now') "
                "where id = ? and task_status = ? and terminate_requested = 0",
                (TaskStatus.RUNNING, worker, now, now, token, task_id, TaskStatus.QUEUED),
            )
            if cur.rowcount != 1:
                return False
            if event is not None:
                insert_event(conn, task_id, event)
            return True

    def transition(
        self,
        task_id: int,
        fields: Mapping[str, Any],
        *,
        expect: Iterable[TaskStatus],
        require: Mapping[str, Any] | None = None,
        events: Sequence[Event] = (),
    ) -> bool:
        """Compare-and-set a task's lifecycle fields; for the state machine only.

        Applies ``fields`` only if the task's status is one of ``expect`` and every
        ``require`` column still holds the given value (``None`` means null), then
        records ``events`` in the same transaction. Returns whether it applied; a
        False means another writer changed the task first.
        """
        expect = tuple(expect)
        if not expect:
            raise ValueError("transition needs at least one expected status")
        bad = set(fields) - LIFECYCLE_COLUMNS - UPDATABLE_COLUMNS
        bad |= set(require or {}) - LIFECYCLE_COLUMNS - UPDATABLE_COLUMNS
        if bad:
            raise ValueError(f"columns not writable by a transition: {sorted(bad)}")
        sets = ", ".join(f"{col} = ?" for col in fields)
        where = ["id = ?", f"task_status in ({','.join('?' * len(expect))})"]
        params: list[Any] = [*fields.values(), task_id, *expect]
        for col, value in (require or {}).items():
            if value is None:
                where.append(f"{col} is null")
            else:
                where.append(f"{col} = ?")
                params.append(value)
        prefix = f"{sets}, " if sets else ""
        with self.db.connect() as conn:
            cur = conn.execute(
                f"update tasks set {prefix}updated_at = datetime('now') "
                f"where {' and '.join(where)}",
                params,
            )
            if cur.rowcount != 1:
                return False
            for event in events:
                insert_event(conn, task_id, event)
            return True

    def heartbeat(
        self, task_id: int, *, at: str, progress: Any = _UNSET, token: str | None = None
    ) -> bool:
        """Refresh a running task's heartbeat (and optionally its progress).

        Only touches a task that is still being executed (RUNNING or PAUSE_PENDING)
        and, given a ``token``, still held by that claim, so a stale worker cannot
        keep alive a task that has been reclaimed and handed to someone else.
        """
        sets, params = ["heartbeat_at = ?"], [at]
        if progress is not _UNSET:
            sets.append("progress = ?")
            params.append(progress)
        where = "id = ? and task_status in (?, ?)"
        params += [task_id, TaskStatus.RUNNING, TaskStatus.PAUSE_PENDING]
        if token is not None:
            where += " and claim_token = ?"
            params.append(token)
        with self.db.connect() as conn:
            cur = conn.execute(f"update tasks set {', '.join(sets)} where {where}", params)
            return cur.rowcount == 1

    def update_fields(
        self, task_id: int, fields: dict[str, Any], *, token: str | None = None
    ) -> bool:
        """Write non-lifecycle columns (name, priority, max_retries, progress).

        Status, step, locks and retry bookkeeping are lifecycle columns: they
        change only through the state machine (``transition``). Given a claim
        ``token``, writes only while that claim still holds the task (a worker's
        own updates). Returns whether a row was written.
        """
        # Empty fields is a no-op; a missing task_id affects 0 rows by standard
        # SQL semantics (no error). Existence checks belong to the StateMachine;
        # the repository does pure CRUD only.
        if not fields:
            return False
        bad = set(fields) - UPDATABLE_COLUMNS
        if bad:
            lifecycle = sorted(bad & LIFECYCLE_COLUMNS)
            hint = f" ({', '.join(lifecycle)}: use the state machine)" if lifecycle else ""
            raise ValueError(f"columns not updatable: {sorted(bad)}{hint}")
        assignments = ", ".join(f"{col} = ?" for col in fields)
        values = [_dump(v) if col == "metadata" else v for col, v in fields.items()]
        values.append(task_id)
        where = "id = ?"
        if token is not None:
            where += " and claim_token = ?"
            values.append(token)
        with self.db.connect() as conn:
            cur = conn.execute(
                f"update tasks set {assignments}, updated_at = datetime('now') where {where}",
                values,
            )
            return cur.rowcount == 1


def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    task = dict(row)
    if "metadata" in task:
        task["metadata"] = json.loads(task["metadata"]) if task["metadata"] else {}
    return task


def _dump(metadata: Any) -> str | None:
    if metadata is None:
        return None
    if not isinstance(metadata, dict):
        raise ValueError("metadata must be a JSON object (a dict)")
    return json.dumps(metadata, ensure_ascii=False, sort_keys=True)
