"""The state machine at a step boundary: outcomes, flags, tokens, contention."""

from __future__ import annotations

import pytest

from runspool.clock import utcnow_text
from runspool.models import EventType, TaskStatus, WorkflowDef
from runspool.persistence.connection import Database
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository
from runspool.persistence.state_machine import (
    Deferred,
    Failed,
    IllegalTransition,
    StateMachine,
    Succeeded,
)
from runspool.persistence.step_run_log import StepRunLog
from tests.support import force_fields


@pytest.fixture
def env(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init()
    repo, log, runs = TaskRepository(db), EventLog(db), StepRunLog(db)
    sm = StateMachine(repo, log, workflow=WorkflowDef("wf", ["a", "b"]), step_runs=runs)

    def new_task(*, max_retries: int = 2, claim: bool = True, token: str | None = "t1") -> int:
        tid = repo.create_task(input="in", workflow="wf", first_step="a", max_retries=max_retries)
        if claim:
            assert sm.claim(tid, worker="w", token=token)
        return tid

    return sm, repo, log, runs, new_task


def status(repo, tid):
    return repo.get_task(tid)["task_status"]


def event_types(log, tid):
    return [e["event_type"] for e in reversed(log.list_for_task(tid))]


def run_step(runs, tid, step, outcome):
    run = runs.start(tid, step)
    runs.finish(run, status=outcome, duration_ms=1)


# -- partially completed -----------------------------------------------------------


def test_a_degraded_step_makes_the_workflow_partially_complete(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    run_step(runs, tid, "a", "degraded")
    sm.finish_step(tid, Succeeded(degraded=True), token="t1")
    assert repo.get_task(tid)["step"] == "b"
    assert sm.claim(tid, worker="w", token="t2")
    run_step(runs, tid, "b", "ok")
    after = sm.finish_step(tid, Succeeded(), token="t2")
    assert after["task_status"] == TaskStatus.PARTIALLY_COMPLETED


def test_a_successful_rerun_clears_an_earlier_degraded_run(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    run_step(runs, tid, "a", "degraded")
    run_step(runs, tid, "a", "ok")
    sm.finish_step(tid, Succeeded(), token="t1")
    assert sm.claim(tid, worker="w", token="t2")
    run_step(runs, tid, "b", "ok")
    assert sm.finish_step(tid, Succeeded(), token="t2")["task_status"] == TaskStatus.COMPLETED


def test_partially_completed_is_terminal(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    force_fields(repo, tid, {"task_status": TaskStatus.PARTIALLY_COMPLETED})
    with pytest.raises(IllegalTransition):
        sm.request_terminate(tid)
    assert repo.find_active_by_input("in") is None


# -- flags -------------------------------------------------------------------------


@pytest.mark.parametrize(
    "outcome", [Succeeded(), Deferred("waiting"), Failed("boom")], ids=["ok", "defer", "fail"]
)
def test_terminate_wins_over_every_outcome(env, outcome):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    sm.request_pause(tid)
    sm.request_terminate(tid)
    after = sm.finish_step(tid, outcome, token="t1")
    assert after["task_status"] == TaskStatus.TERMINATED
    assert (after["pause_requested"], after["terminate_requested"]) == (0, 0)
    assert after["claim_token"] is None
    if isinstance(outcome, Failed):
        assert after["last_error"] == "boom"


def test_pause_after_a_successful_step_advances_then_pauses(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    sm.request_pause(tid)
    after = sm.finish_step(tid, Succeeded(), token="t1")
    assert (after["task_status"], after["step"]) == (TaskStatus.PAUSED, "b")


def test_pause_after_a_deferred_step_pauses_in_place(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    sm.request_pause(tid)
    after = sm.finish_step(tid, Deferred("not yet", delay_seconds=600), token="t1")
    assert (after["task_status"], after["step"], after["next_retry_at"]) == (
        TaskStatus.PAUSED,
        "a",
        None,
    )


def test_pause_after_a_failed_step_records_the_failure_and_pauses(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    sm.request_pause(tid)
    after = sm.finish_step(tid, Failed("boom", retry_delay_seconds=60), token="t1")
    assert (after["task_status"], after["retry_count"], after["last_error"]) == (
        TaskStatus.PAUSED,
        1,
        "boom",
    )
    assert after["next_retry_at"] is None
    assert event_types(log, tid)[-2:] == ["step_failed", "paused"]


def test_exhausted_retries_win_over_a_pause(env):
    sm, repo, log, runs, new_task = env
    tid = new_task(max_retries=0)
    sm.request_pause(tid)
    after = sm.finish_step(tid, Failed("boom"), token="t1")
    assert after["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert after["pause_requested"] == 0


# -- defer with reason and delay ------------------------------------------------------


def test_defer_records_its_reason_and_waits_out_its_delay(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    after = sm.finish_step(tid, Deferred("bilibili not published", delay_seconds=1800), token="t1")
    assert after["task_status"] == TaskStatus.QUEUED
    assert after["next_retry_at"] > utcnow_text()
    deferred = log.list_for_task(tid, limit=1)[0]
    assert (deferred["event_type"], deferred["message"]) == ("deferred", "bilibili not published")


def test_defer_without_delay_is_runnable_at_once(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    assert sm.finish_step(tid, Deferred("soon"), token="t1")["next_retry_at"] is None


def test_wake_makes_a_waiting_task_runnable_now(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    sm.finish_step(tid, Deferred("later", delay_seconds=1800), token="t1")
    sm.wake(tid)
    assert repo.get_task(tid)["next_retry_at"] is None
    assert event_types(log, tid)[-1] == EventType.WOKEN
    with pytest.raises(IllegalTransition):
        sm.wake(tid)  # nothing to wake any more


@pytest.mark.parametrize("action", ["claim", "resume", "retry"])
def test_actions_that_requeue_or_claim_clear_a_pending_delay(env, action):
    sm, repo, log, runs, new_task = env
    tid = new_task(claim=False)
    later = "2999-01-01 00:00:00"
    if action == "claim":
        force_fields(repo, tid, {"next_retry_at": later})
        sm.claim(tid, worker="w")
    elif action == "resume":
        force_fields(repo, tid, {"task_status": TaskStatus.PAUSED, "next_retry_at": later})
        sm.resume(tid)
    else:
        force_fields(repo, tid, {"task_status": TaskStatus.FAILED, "next_retry_at": later})
        sm.retry(tid)
    assert repo.get_task(tid)["next_retry_at"] is None


# -- claim tokens and stale workers ----------------------------------------------------


def test_a_stale_worker_cannot_finish_a_reclaimed_task(env):
    sm, repo, log, runs, new_task = env
    tid = new_task(token="old")
    sm.requeue_stale(tid)
    assert sm.claim(tid, worker="w2", token="new")
    after = sm.finish_step(tid, Succeeded(), token="old")  # the old job wakes up
    assert (after["task_status"], after["step"], after["claim_token"]) == (
        TaskStatus.RUNNING,
        "a",
        "new",
    )


def test_finish_on_a_task_that_is_no_longer_executing_changes_nothing(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    sm.requeue_stale(tid)
    after = sm.finish_step(tid, Failed("late"), token=None)
    assert (after["task_status"], after["retry_count"]) == (TaskStatus.QUEUED, 0)


# -- contention -------------------------------------------------------------------------


def test_a_pause_requested_between_read_and_write_is_not_lost(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    other = StateMachine(repo, log, workflow=sm.workflow, step_runs=runs)
    real = repo.transition
    calls = {"n": 0}

    def racing_transition(task_id, fields, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            other.request_pause(task_id)  # lands after finish_step read the flags
        return real(task_id, fields, **kw)

    repo.transition = racing_transition
    try:
        after = sm.finish_step(tid, Succeeded(), token="t1")
    finally:
        repo.transition = real
    assert (after["task_status"], after["step"]) == (TaskStatus.PAUSED, "b")


# -- recovery ---------------------------------------------------------------------------


def test_recovery_settles_every_executing_task(env):
    sm, repo, log, runs, new_task = env
    plain, paused, terminated = new_task(), new_task(), new_task()
    sm.request_pause(paused)
    sm.request_terminate(terminated)
    sm.recover_interrupted()
    assert [status(repo, t) for t in (plain, paused, terminated)] == [
        TaskStatus.QUEUED,
        TaskStatus.PAUSED,
        TaskStatus.TERMINATED,
    ]
    assert all(repo.get_task(t)["claim_token"] is None for t in (plain, paused, terminated))
    assert event_types(log, plain)[-1] == EventType.RECLAIMED


# -- user actions -------------------------------------------------------------------------


def test_set_step_never_moves_a_running_task(env):
    sm, repo, log, runs, new_task = env
    tid = new_task()
    with pytest.raises(IllegalTransition):
        sm.set_step(tid, "b", force=True)


def test_set_retries_resets_the_count(env):
    sm, repo, log, runs, new_task = env
    tid = new_task(max_retries=0)
    sm.finish_step(tid, Failed("boom"), token="t1")
    sm.set_retries(tid, 3)
    task = repo.get_task(tid)
    assert (task["max_retries"], task["retry_count"]) == (3, 0)
    with pytest.raises(ValueError):
        sm.set_retries(tid, -1)
