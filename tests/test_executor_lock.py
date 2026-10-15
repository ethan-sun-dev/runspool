"""The executor lock: one process executes steps on a database at a time; recovery
and schema upgrades happen only in the holder; the holder never reclaims its own
running work, however stale its heartbeat looks."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

import pytest

from runspool.app import load_context
from runspool.engine.step import Step, StepContext, StepResult
from runspool.executor_lock import ExecutorBusy, ExecutorLock, executor_lock_path
from runspool.models import TaskStatus
from runspool.persistence.connection import Database
from runspool.persistence.schema import SCHEMA_VERSION
from runspool.runtime import run_until_idle
from tests.conftest import write_config
from tests.engine.test_coordinator import _coordinator
from tests.support import force_fields

_HOLD = """
import sys
from runspool.executor_lock import ExecutorLock
lock = ExecutorLock(sys.argv[1])
assert lock.acquire()
print("held", flush=True)
sys.stdin.read()
"""


@contextmanager
def held_elsewhere(path: Path):
    """Hold the lock in another process until the block ends."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _HOLD, str(path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout.readline().strip() == "held"
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=10)


def test_one_holder_at_a_time(tmp_path):
    path = tmp_path / "x.db.executor-lock"
    first, second = ExecutorLock(path), ExecutorLock(path)
    assert first.acquire()
    assert first.holder() == os.getpid()
    assert not second.acquire()  # the lock is per open file, even in one process
    with pytest.raises(ExecutorBusy, match=str(os.getpid())):
        with second:
            pass
    first.release()
    assert first.holder() is None
    with second:
        assert second.held
    assert not second.held


def test_the_lock_goes_away_with_its_process(tmp_path):
    path = tmp_path / "x.db.executor-lock"
    with held_elsewhere(path) as proc:
        assert ExecutorLock(path).holder() == proc.pid
        assert not ExecutorLock(path).acquire()
    # The holder exited without releasing: the OS dropped the lock.
    lock = ExecutorLock(path)
    assert lock.acquire()
    lock.release()


def test_the_lock_sits_next_to_the_database(tmp_path):
    assert executor_lock_path(tmp_path / "runspool.db") == tmp_path / "runspool.db.executor-lock"


def test_run_refuses_while_another_process_executes(tmp_path):
    ctx = load_context(write_config(tmp_path))
    tid = ctx.service("tasks").add("hello", workflow="local_file")
    # A task another process is running: recovery must leave it alone.
    force_fields(ctx.repo, tid, {"task_status": TaskStatus.RUNNING, "claim_token": "theirs"})
    with held_elsewhere(executor_lock_path(ctx.config.database_path)):
        with pytest.raises(ExecutorBusy, match="cannot run"):
            run_until_idle(ctx, notifier=lambda m: None)
        task = ctx.repo.get_task(tid)
        assert task["task_status"] == TaskStatus.RUNNING
        assert task["claim_token"] == "theirs"


def test_run_takes_and_releases_the_lock(tmp_path):
    ctx = load_context(write_config(tmp_path))
    ctx.service("tasks").add("hello", workflow="local_file")
    run_until_idle(ctx, notifier=lambda m: None)
    lock = ExecutorLock(executor_lock_path(ctx.config.database_path))
    assert lock.acquire()
    lock.release()


def _make_old(db_path: Path) -> None:
    """Mark the schema one version back (migrations are idempotent, so re-running
    the last one is harmless)."""
    db = Database(db_path)
    db.init()
    with db.connect() as conn:
        conn.execute(
            "update schema_meta set value = ? where key = 'version'", (str(SCHEMA_VERSION - 1),)
        )


def test_a_schema_upgrade_waits_for_the_executor(tmp_path):
    db_path = tmp_path / "runspool.db"
    lock_path = executor_lock_path(db_path)
    _make_old(db_path)
    with held_elsewhere(lock_path):
        with pytest.raises(ExecutorBusy, match="upgrade the database schema"):
            Database(db_path).init(upgrade_lock=lock_path)
    assert Database(db_path).init(upgrade_lock=lock_path) == SCHEMA_VERSION


def test_an_up_to_date_database_opens_while_the_executor_runs(tmp_path):
    db_path = tmp_path / "runspool.db"
    lock_path = executor_lock_path(db_path)
    Database(db_path).init()
    with held_elsewhere(lock_path):
        assert Database(db_path).init(upgrade_lock=lock_path) == SCHEMA_VERSION


def test_the_holder_may_upgrade_its_own_database(tmp_path):
    db_path = tmp_path / "runspool.db"
    lock_path = executor_lock_path(db_path)
    _make_old(db_path)
    with ExecutorLock(lock_path):
        assert Database(db_path).init(upgrade_lock=lock_path) == SCHEMA_VERSION


class _Wait(Step):
    name = "alpha"

    def __init__(self) -> None:
        import threading

        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, ctx: StepContext) -> StepResult:
        self.started.set()
        self.release.wait(10)
        return StepResult(message="ok")


def test_the_holder_never_reclaims_work_it_is_running(tmp_path):
    # As after the machine wakes from sleep: the step is alive in this process, but
    # its heartbeat is older than the timeout until the heartbeat thread catches up.
    step = _Wait()
    ctx, coord, pool = _coordinator(tmp_path, step)
    tid = ctx.repo.create_task(input="x", workflow="local_file", first_step="alpha", max_retries=0)
    assert coord.tick() == 1
    assert step.started.wait(5)
    token = ctx.repo.get_task(tid)["claim_token"]
    force_fields(ctx.repo, tid, {"heartbeat_at": "2000-01-01 00:00:00"})

    coord.reclaim_stale(60)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.RUNNING
    assert task["claim_token"] == token

    step.release.set()
    pool.drain()
    assert ctx.repo.get_task(tid)["step"] == "beta"

    # Work that is not running here is still reclaimed when stale.
    other = ctx.repo.create_task(
        input="y", workflow="local_file", first_step="alpha", max_retries=0
    )
    force_fields(
        ctx.repo,
        other,
        {
            "task_status": TaskStatus.RUNNING,
            "claim_token": "gone",
            "heartbeat_at": "2000-01-01 00:00:00",
        },
    )
    coord.reclaim_stale(60)
    assert ctx.repo.get_task(other)["task_status"] == TaskStatus.QUEUED
    pool.shutdown()


def test_the_run_command_reports_a_busy_executor(tmp_path):
    from typer.testing import CliRunner

    from runspool.cli import app

    cfg = write_config(tmp_path)
    ctx = load_context(cfg)
    with held_elsewhere(executor_lock_path(ctx.config.database_path)) as proc:
        result = CliRunner().invoke(app, ["-c", str(cfg), "run"])
    assert result.exit_code == 1
    assert f"pid {proc.pid}" in result.output
