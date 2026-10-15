"""Versioned schema migrations."""

from __future__ import annotations

import sqlite3
from pathlib import Path

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


# -- version 4: artifacts ------------------------------------------------------------

FIXTURES = Path(__file__).parent / "fixtures"


def _v3_database(path):
    """The database RunSpool 0.2.2 left behind (see the fixture's header)."""
    with sqlite3.connect(path) as conn:
        conn.executescript((FIXTURES / "v3_runspool_0_2_2.sql").read_text(encoding="utf-8"))
    return path


def _rows(path, sql):
    with sqlite3.connect(path) as conn:
        return conn.execute(sql).fetchall()


def test_a_v3_database_from_0_2_2_is_upgraded_to_v4_keeping_everything(tmp_path):
    path = _v3_database(tmp_path / "v3.db")
    tables = ("tasks", "task_events", "step_runs")
    before = {t: _rows(path, f"select * from {t} order by id") for t in tables}
    columns_before = {t: columns(path, t) for t in tables}

    assert SCHEMA_VERSION == 4
    assert Database(path).init() == 4
    assert _rows(path, "select value from schema_meta where key = 'version'") == [("4",)]
    for t in tables:  # only a table was added: existing tables and rows are untouched
        assert columns(path, t) == columns_before[t]
        assert _rows(path, f"select * from {t} order by id") == before[t]
    assert columns(path, "artifacts") == {
        "id", "task_id", "step", "step_run_id", "type", "path", "sha256", "size_bytes",
        "created_at",
    }
    assert _rows(path, "select count(*) from artifacts") == [(0,)]

    task = TaskRepository(Database(path)).get_task(1)
    assert task["name"] == "笔记 一" and task["task_status"] == "completed"


def test_the_upgraded_database_records_artifacts(tmp_path):
    from runspool.engine.artifacts import RecordedArtifact
    from runspool.persistence.artifact_log import ArtifactLog

    db = Database(_v3_database(tmp_path / "v3.db"))
    db.init()
    log = ArtifactLog(db)
    log.record(1, "archive", [RecordedArtifact("doc", "ready/1/笔记 一.md", "ab" * 32, 10)])
    (row,) = log.list_for_task(1)  # step_run_id None: not tied to a run, always listed
    assert (row["path"], row["sha256"]) == ("ready/1/笔记 一.md", "ab" * 32)


def test_migration_4_can_be_applied_again_after_an_interruption(tmp_path):
    from runspool.persistence.schema import MIGRATIONS

    db = Database(tmp_path / "t.db")
    db.init()
    (apply_v4,) = [fn for version, fn in MIGRATIONS if version == 4]
    with sqlite3.connect(db.path) as conn:
        apply_v4(conn)  # tables and indexes already there: a no-op
        conn.execute("update schema_meta set value = '3' where key = 'version'")
    assert db.init() == 4
