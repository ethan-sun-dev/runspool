"""SQLite schema and its versioned migrations.

``schema_meta`` records the schema version. Opening a database applies every
migration it has not seen, in order; each one is idempotent, so a migration
interrupted by a crash is simply re-applied next time. A database newer than this
RunSpool is refused rather than written with an older layout.

Version 1 is the baseline: it creates the tables for a fresh database and brings a
database from before versioning (0.1, or 0.2 before migrations) up to the same
columns. Later versions add to it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable

BASE_SCHEMA = """
create table if not exists tasks (
    id integer primary key autoincrement,
    input text not null,
    name text,
    workflow text not null,
    step text not null,
    task_status text not null,
    priority integer not null default 0,
    retry_count integer not null default 0,
    max_retries integer not null default 3,
    locked_by text,
    locked_at text,
    heartbeat_at text,
    pause_requested integer not null default 0,
    terminate_requested integer not null default 0,
    last_error text,
    next_retry_at text,
    created_at text not null default (datetime('now')),
    updated_at text not null default (datetime('now')),
    progress text,
    claim_token text
);

create table if not exists task_events (
    id integer primary key autoincrement,
    task_id integer not null references tasks(id),
    event_type text not null,
    step text,
    message text,
    payload_json text,
    created_at text not null default (datetime('now'))
);

-- step_runs records the duration / outcome / error of each step execution.
create table if not exists step_runs (
    id integer primary key autoincrement,
    task_id integer not null references tasks(id),
    step text not null,
    status text not null,
    started_at text not null default (datetime('now')),
    finished_at text,
    duration_ms integer,
    error text,
    note text
);

create index if not exists idx_tasks_status on tasks(task_status);
create index if not exists idx_events_task on task_events(task_id);
create index if not exists idx_runs_task on step_runs(task_id);
"""


class SchemaTooNew(RuntimeError):
    """The database was written by a newer RunSpool."""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"pragma table_info({table})").fetchall()}


def _add_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    if column not in _columns(conn, table):
        conn.execute(f"alter table {table} add column {column} {decl}")


def _v1_baseline(conn: sqlite3.Connection) -> None:
    # One statement at a time: executescript would commit the surrounding transaction.
    for statement in BASE_SCHEMA.split(";"):
        if statement.strip():
            conn.execute(statement)
    # Columns added before versioning existed: backfill them on older databases.
    _add_column(conn, "tasks", "progress", "text")
    _add_column(conn, "tasks", "name", "text")
    _add_column(conn, "tasks", "claim_token", "text")
    _add_column(conn, "step_runs", "note", "text")


def _v2_metadata_and_parent(conn: sqlite3.Connection) -> None:
    # metadata: free-form JSON for plugins (e.g. a sub-flow's source paths).
    # parent_task_id: the task this one was spawned from (sub-flows).
    _add_column(conn, "tasks", "metadata", "text")
    _add_column(conn, "tasks", "parent_task_id", "integer references tasks(id)")
    conn.execute("create index if not exists idx_tasks_parent on tasks(parent_task_id)")


def _v3_approval(conn: sqlite3.Connection) -> None:
    # approval_grant: "asked:<step>:<attempt>" while waiting, "granted:<step>:<attempt>"
    # once approved; a grant is good for that one attempt of that one step.
    _add_column(conn, "tasks", "approval_grant", "text")


def _v4_artifacts(conn: sqlite3.Connection) -> None:
    # artifacts: what steps recorded producing (StepResult.artifacts), one row each.
    # step_run_id ties a row to the run that produced it, so readers can ignore rows
    # from a run that did not finish (see runspool.persistence.artifact_log).
    conn.execute(
        """
        create table if not exists artifacts (
            id integer primary key autoincrement,
            task_id integer not null references tasks(id),
            step text not null,
            step_run_id integer references step_runs(id),
            type text not null,
            path text not null,
            sha256 text not null,
            size_bytes integer,
            created_at text not null default (datetime('now'))
        )
        """
    )
    conn.execute("create index if not exists idx_artifacts_task on artifacts(task_id)")


def _v5_work_items(conn: sqlite3.Connection) -> None:
    # work_items: the work an external step (runspool.engine.external) asks for, one
    # row per attempt. status: open -> claimed -> submitted -> accepted | rejected |
    # exhausted (rejected, no attempts left); cancelled when the task moves on
    # without it. At most one item per task is active (open, claimed or submitted).
    conn.execute(
        """
        create table if not exists work_items (
            id integer primary key autoincrement,
            task_id integer not null references tasks(id),
            step text not null,
            attempt integer not null default 1,
            max_attempts integer not null,
            status text not null,
            kind text not null,
            instructions text not null default '',
            payload_json text,
            previous_error text,
            claimed_by text,
            claim_token text,
            claimed_at text,
            submission_json text,
            submitted_by text,
            submitted_at text,
            error text,
            step_run_id integer references step_runs(id),
            created_at text not null default (datetime('now')),
            updated_at text not null default (datetime('now'))
        )
        """
    )
    conn.execute("create index if not exists idx_work_items_status on work_items(status)")
    conn.execute("create index if not exists idx_work_items_task on work_items(task_id)")
    conn.execute(
        "create unique index if not exists idx_work_items_active on work_items(task_id) "
        "where status in ('open', 'claimed', 'submitted')"
    )


def _v6_task_spawns(conn: sqlite3.Connection) -> None:
    # task_spawns: which step (run) of a parent spawned which child task
    # (StepResult.spawn). A non-null key names a child within its parent: spawning
    # the same key again does not create a second child.
    conn.execute(
        """
        create table if not exists task_spawns (
            id integer primary key autoincrement,
            parent_task_id integer not null references tasks(id),
            child_task_id integer not null references tasks(id),
            step text not null,
            step_run_id integer references step_runs(id),
            spawn_key text,
            created_at text not null default (datetime('now'))
        )
        """
    )
    conn.execute("create index if not exists idx_task_spawns_parent on task_spawns(parent_task_id)")
    conn.execute(
        "create unique index if not exists idx_task_spawns_key on "
        "task_spawns(parent_task_id, spawn_key) where spawn_key is not null"
    )


MIGRATIONS: list[tuple[int, Callable[[sqlite3.Connection], None]]] = [
    (1, _v1_baseline),
    (2, _v2_metadata_and_parent),
    (3, _v3_approval),
    (4, _v4_artifacts),
    (5, _v5_work_items),
    (6, _v6_task_spawns),
]
SCHEMA_VERSION = MIGRATIONS[-1][0]


def stored_version(conn: sqlite3.Connection) -> int:
    """The schema version recorded in the database (0: new or from before versioning)."""
    try:
        row = conn.execute("select value from schema_meta where key = 'version'").fetchone()
    except sqlite3.OperationalError:  # no schema_meta table yet
        return 0
    return int(row[0]) if row else 0


def migrate(conn: sqlite3.Connection) -> int:
    """Bring the database up to ``SCHEMA_VERSION``; return the version it ends at."""
    conn.execute(
        "create table if not exists schema_meta (key text primary key, value text not null)"
    )
    row = conn.execute("select value from schema_meta where key = 'version'").fetchone()
    version = int(row[0]) if row else 0
    if version > SCHEMA_VERSION:
        raise SchemaTooNew(
            f"database schema version {version} is newer than this RunSpool supports "
            f"({SCHEMA_VERSION}); upgrade RunSpool"
        )
    for target, apply in MIGRATIONS:
        if version >= target:
            continue
        apply(conn)
        conn.execute(
            "insert into schema_meta (key, value) values ('version', ?) "
            "on conflict(key) do update set value = excluded.value",
            (str(target),),
        )
        version = target
    return version
