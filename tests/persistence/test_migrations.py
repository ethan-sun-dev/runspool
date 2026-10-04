"""Versioned schema migrations."""

from __future__ import annotations

import sqlite3

import pytest

from runspool.persistence.connection import Database
from runspool.persistence.repository import TaskRepository
from runspool.persistence.schema import SCHEMA_VERSION, SchemaTooNew

# A database as RunSpool 0.1.0 created it: no schema_meta, no later columns.
V01_SCHEMA = """
create table tasks (
    id integer primary key autoincrement, input text not null, workflow text not null,
    step text not null, task_status text not null, priority integer not null default 0,
    retry_count integer not null default 0, max_retries integer not null default 3,
    locked_by text, locked_at text, heartbeat_at text,
    pause_requested integer not null default 0, terminate_requested integer not null default 0,
    last_error text, next_retry_at text,
    created_at text not null default (datetime('now')),
    updated_at text not null default (datetime('now'))
);
create table task_events (
    id integer primary key autoincrement, task_id integer not null references tasks(id),
    event_type text not null, step text, message text, payload_json text,
    created_at text not null default (datetime('now'))
);
create table step_runs (
    id integer primary key autoincrement, task_id integer not null references tasks(id),
    step text not null, status text not null,
    started_at text not null default (datetime('now')), finished_at text,
    duration_ms integer, error text
);
"""


def columns(path, table):
    with sqlite3.connect(path) as conn:
        return {r[1] for r in conn.execute(f"pragma table_info({table})")}


def test_a_fresh_database_is_created_at_the_current_version(tmp_path):
    assert Database(tmp_path / "new.db").init() == SCHEMA_VERSION


def test_a_01_database_is_upgraded_in_place_and_keeps_its_tasks(tmp_path):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.executescript(V01_SCHEMA)
        conn.execute(
            "insert into tasks (input, workflow, step, task_status) "
            "values ('a.txt', 'wf', 'x', 'queued')"
        )
    assert Database(path).init() == SCHEMA_VERSION
    assert {"progress", "name", "claim_token", "metadata", "parent_task_id", "approval_grant"} <= (
        columns(path, "tasks")
    )
    assert "note" in columns(path, "step_runs")
    task = TaskRepository(Database(path)).get_task(1)
    assert (task["input"], task["metadata"], task["parent_task_id"]) == ("a.txt", {}, None)


def test_init_is_idempotent(tmp_path):
    db = Database(tmp_path / "t.db")
    assert db.init() == db.init() == SCHEMA_VERSION


def test_a_database_from_a_newer_runspool_is_refused(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init()
    with sqlite3.connect(db.path) as conn:
        conn.execute(
            "update schema_meta set value = ? where key = 'version'", (SCHEMA_VERSION + 1,)
        )
    with pytest.raises(SchemaTooNew, match="upgrade RunSpool"):
        db.init()
