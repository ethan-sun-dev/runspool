"""Races found in review of M3: requests that land while the machine is deciding.

Each test runs a second actor at the exact moment between the machine's read and
its compare-and-set, the way another process (the CLI next to a daemon) could.
"""

from __future__ import annotations

import pytest

from runspool.models import TaskStatus, WorkflowDef
from runspool.persistence.connection import Database
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository
from runspool.persistence.state_machine import Failed, IllegalTransition, StateMachine
from runspool.persistence.step_run_log import StepRunLog
from tests.support import force_fields

OLD = "2000-01-01 00:00:00"


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init()
    repo, log, runs = TaskRepository(db), EventLog(db), StepRunLog(db)
    wf = WorkflowDef("wf", ["a", "b"])

    def machine():
        return StateMachine(repo, log, workflow=wf, step_runs=runs)

    def new_task(*, max_retries: int = 1, token: str = "T") -> int:
        tid = repo.create_task(input="in", workflow="wf", first_step="a", max_retries=max_retries)
        assert machine().claim(tid, worker="w", token=token)
        return tid

    return machine, repo, new_task


def after_reads(repo, n, action):
    """Run ``action`` right after the n-th ``get_task`` / ``list_by_status`` read."""
    counter = {"n": 0}
    real_get, real_list = repo.get_task, repo.list_by_status

    def tick():
        counter["n"] += 1
        if counter["n"] == n:
            action()

    def get_task(task_id):
        row = real_get(task_id)
        tick()
        return row

    def list_by_status(status):
        rows = real_list(status)
        tick()
        return rows

    repo.get_task, repo.list_by_status = get_task, list_by_status

    def restore():
        repo.get_task, repo.list_by_status = real_get, real_list

    return restore


def test_set_retries_landing_mid_finish_is_respected(env):
    machine, repo, new_task = env
    tid = new_task(max_retries=1)
    force_fields(repo, tid, {"retry_count": 1})
    restore = after_reads(repo, 1, lambda: machine().set_retries(tid, 5))
    try:
        machine().finish_step(tid, Failed("boom"), token="T")
    finally:
        restore()
    task = repo.get_task(tid)
    assert (task["task_status"], task["retry_count"], task["max_retries"]) == (
        TaskStatus.FAILED,
        1,
        5,
    )


def test_reclaiming_a_stale_task_honours_its_terminate_request(env):
    machine, repo, new_task = env
    tid = new_task()
    machine().request_terminate(tid)
    force_fields(repo, tid, {"heartbeat_at": OLD})
    machine().requeue_stale(tid)
    task = repo.get_task(tid)
    assert task["task_status"] == TaskStatus.TERMINATED
    assert task["terminate_requested"] == 0


def test_reclaiming_a_stale_pausing_task_pauses_it(env):
    machine, repo, new_task = env
    tid = new_task()
    machine().request_pause(tid)
    force_fields(repo, tid, {"heartbeat_at": OLD})
    assert [t["id"] for t in repo.list_stale_running(60)] == [tid]
    machine().requeue_stale(tid)
    task = repo.get_task(tid)
    assert (task["task_status"], task["claim_token"]) == (TaskStatus.PAUSED, None)
    machine().resume(tid)  # and it can be resumed again, instead of being stuck


def test_a_terminate_landing_during_recovery_is_not_lost(env):
    machine, repo, new_task = env
    tid = new_task()
    restore = after_reads(repo, 1, lambda: machine().request_terminate(tid))
    try:
        machine().recover_interrupted()
    finally:
        restore()
    assert repo.get_task(tid)["task_status"] == TaskStatus.TERMINATED


def test_a_terminate_landing_during_a_stale_reclaim_is_not_lost(env):
    machine, repo, new_task = env
    tid = new_task()
    force_fields(repo, tid, {"heartbeat_at": OLD})
    restore = after_reads(repo, 1, lambda: machine().request_terminate(tid))
    try:
        machine().requeue_stale(tid)
    finally:
        restore()
    assert repo.get_task(tid)["task_status"] == TaskStatus.TERMINATED


def test_a_queued_task_left_with_a_terminate_flag_is_terminated_not_run(env):
    machine, repo, new_task = env
    tid = repo.create_task(input="in", workflow="wf", first_step="a", max_retries=1)
    force_fields(repo, tid, {"terminate_requested": 1})  # e.g. left by an older version
    assert not machine().claim(tid, worker="w", token="T")
    assert repo.get_task(tid)["task_status"] == TaskStatus.TERMINATED


def test_a_fresh_heartbeat_between_listing_and_reclaim_keeps_the_task(env):
    machine, repo, new_task = env
    tid = new_task()
    force_fields(repo, tid, {"heartbeat_at": OLD})
    restore = after_reads(repo, 1, lambda: repo.heartbeat(tid, at="2999-01-01 00:00:00", token="T"))
    try:
        machine().requeue_stale(tid)
    finally:
        restore()
    assert repo.get_task(tid)["task_status"] == TaskStatus.RUNNING


def test_set_step_refuses_finished_tasks_even_when_forced(env):
    machine, repo, new_task = env
    tid = new_task()
    force_fields(repo, tid, {"task_status": TaskStatus.COMPLETED})
    with pytest.raises(IllegalTransition):
        machine().set_step(tid, "a", force=True)
