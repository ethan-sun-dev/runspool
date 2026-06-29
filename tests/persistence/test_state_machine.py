import pytest

from runspool.models import TaskStatus, WorkflowDef
from runspool.persistence.connection import Database
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository
from runspool.persistence.state_machine import IllegalTransition, StateMachine


def _setup(tmp_path, steps=("a", "b"), max_retries=1):
    db = Database(tmp_path / "t.db")
    db.init()
    repo = TaskRepository(db)
    log = EventLog(db)
    sm = StateMachine(repo, log, workflow=WorkflowDef("w", list(steps)))
    tid = repo.create_task(input="x", workflow="w", first_step=steps[0], max_retries=max_retries)
    return repo, sm, tid


def test_claim_then_complete_advances(tmp_path):
    repo, sm, tid = _setup(tmp_path)
    assert sm.claim(tid, worker="w1") is True
    assert repo.get_task(tid)["task_status"] == TaskStatus.RUNNING
    sm.complete_step(tid)
    task = repo.get_task(tid)
    assert task["step"] == "b"
    assert task["task_status"] == TaskStatus.QUEUED


def test_complete_last_step_completes(tmp_path):
    repo, sm, tid = _setup(tmp_path, steps=("only",))
    sm.claim(tid, worker="w1")
    sm.complete_step(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.COMPLETED


def test_double_claim_returns_false(tmp_path):
    repo, sm, tid = _setup(tmp_path)
    assert sm.claim(tid, worker="w1") is True
    assert sm.claim(tid, worker="w2") is False


def test_fail_retries_then_manual_required(tmp_path):
    repo, sm, tid = _setup(tmp_path, max_retries=1)
    sm.claim(tid, worker="w1")
    sm.fail(tid, "boom")
    assert repo.get_task(tid)["task_status"] == TaskStatus.FAILED
    sm.retry(tid)
    sm.claim(tid, worker="w1")
    sm.fail(tid, "boom again")
    assert repo.get_task(tid)["task_status"] == TaskStatus.MANUAL_REQUIRED


def test_fail_schedules_retry_and_requeue_failed(tmp_path):
    repo, sm, tid = _setup(tmp_path, max_retries=2)
    sm.claim(tid, worker="w1")
    sm.fail(tid, "boom")
    task = repo.get_task(tid)
    assert task["task_status"] == TaskStatus.FAILED
    assert task["next_retry_at"] is not None        # auto-retry scheduled
    sm.requeue_failed(tid)
    task = repo.get_task(tid)
    assert task["task_status"] == TaskStatus.QUEUED
    assert task["next_retry_at"] is None


def test_requeue_failed_ignores_non_failed(tmp_path):
    repo, sm, tid = _setup(tmp_path)
    sm.claim(tid, worker="w1")                       # task is RUNNING, not FAILED
    sm.requeue_failed(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.RUNNING


def test_manual_required_clears_next_retry_at(tmp_path):
    repo, sm, tid = _setup(tmp_path, max_retries=0)
    sm.claim(tid, worker="w1")
    sm.fail(tid, "boom")
    task = repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert task["next_retry_at"] is None


def test_pause_resume_cycle(tmp_path):
    repo, sm, tid = _setup(tmp_path)
    sm.claim(tid, worker="w1")
    sm.request_pause(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.PAUSE_PENDING
    sm.apply_pause(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.PAUSED
    sm.resume(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.QUEUED


def test_terminate_flag_then_apply(tmp_path):
    repo, sm, tid = _setup(tmp_path)
    sm.claim(tid, worker="w1")
    sm.request_terminate(tid)
    assert repo.get_task(tid)["terminate_requested"] == 1
    sm.apply_terminate(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.TERMINATED


def test_defer_keeps_step_and_requeues(tmp_path):
    repo, sm, tid = _setup(tmp_path)
    sm.claim(tid, worker="w1")
    sm.defer(tid)
    task = repo.get_task(tid)
    assert task["step"] == "a"
    assert task["task_status"] == TaskStatus.QUEUED
    assert task["retry_count"] == 0


def _force_status(repo, tid, status):
    repo.update_fields(tid, {"task_status": status})


def test_retry_rejected_on_completed(tmp_path):
    repo, sm, tid = _setup(tmp_path)
    _force_status(repo, tid, TaskStatus.COMPLETED)
    with pytest.raises(IllegalTransition):
        sm.retry(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.COMPLETED


def test_pause_rejected_on_completed(tmp_path):
    repo, sm, tid = _setup(tmp_path)
    _force_status(repo, tid, TaskStatus.COMPLETED)
    with pytest.raises(IllegalTransition):
        sm.request_pause(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.COMPLETED


def test_resume_rejected_when_not_paused(tmp_path):
    repo, sm, tid = _setup(tmp_path)  # task is QUEUED
    with pytest.raises(IllegalTransition):
        sm.resume(tid)


def test_terminate_rejected_on_terminal(tmp_path):
    repo, sm, tid = _setup(tmp_path)
    for terminal in (TaskStatus.COMPLETED, TaskStatus.TERMINATED):
        _force_status(repo, tid, terminal)
        with pytest.raises(IllegalTransition):
            sm.request_terminate(tid)
        assert repo.get_task(tid)["task_status"] == terminal


def test_terminate_allowed_on_failed(tmp_path):
    repo, sm, tid = _setup(tmp_path)
    _force_status(repo, tid, TaskStatus.FAILED)
    sm.request_terminate(tid)  # should not raise
    assert repo.get_task(tid)["task_status"] == TaskStatus.TERMINATED


def test_recover_interrupted_requeues_running(tmp_path):
    repo, sm, tid = _setup(tmp_path)
    sm.claim(tid, worker="w1")
    sm.recover_interrupted()
    assert repo.get_task(tid)["task_status"] == TaskStatus.QUEUED
