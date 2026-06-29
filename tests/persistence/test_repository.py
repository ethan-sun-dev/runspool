import threading

import pytest

from runspool.models import TaskStatus
from runspool.persistence.connection import Database
from runspool.persistence.repository import TaskRepository


def _repo(tmp_path):
    db = Database(tmp_path / "t.db")
    db.init()
    return TaskRepository(db)


def test_create_and_get(tmp_path):
    repo = _repo(tmp_path)
    tid = repo.create_task(input="in", workflow="w", first_step="a", max_retries=3, name="N")
    task = repo.get_task(tid)
    assert task["input"] == "in"
    assert task["name"] == "N"
    assert task["task_status"] == TaskStatus.QUEUED
    assert task["step"] == "a"


def test_list_by_status_orders_by_priority(tmp_path):
    repo = _repo(tmp_path)
    low = repo.create_task(input="a", workflow="w", first_step="s", max_retries=0)
    high = repo.create_task(input="b", workflow="w", first_step="s", max_retries=0)
    repo.update_fields(high, {"priority": 10})
    ids = [t["id"] for t in repo.list_by_status(TaskStatus.QUEUED)]
    assert ids[0] == high and low in ids


def test_find_active_by_input_ignores_terminal(tmp_path):
    repo = _repo(tmp_path)
    tid = repo.create_task(input="dup", workflow="w", first_step="s", max_retries=0)
    assert repo.find_active_by_input("dup")["id"] == tid
    repo.update_fields(tid, {"task_status": TaskStatus.COMPLETED})
    assert repo.find_active_by_input("dup") is None


def test_update_rejects_unknown_column(tmp_path):
    repo = _repo(tmp_path)
    tid = repo.create_task(input="a", workflow="w", first_step="s", max_retries=0)
    with pytest.raises(ValueError):
        repo.update_fields(tid, {"not_a_column": 1})


def test_input_is_immutable(tmp_path):
    repo = _repo(tmp_path)
    tid = repo.create_task(input="a", workflow="w", first_step="s", max_retries=0)
    with pytest.raises(ValueError):
        repo.update_fields(tid, {"input": "hacked"})


def test_claim_queued_is_conditional(tmp_path):
    repo = _repo(tmp_path)
    tid = repo.create_task(input="a", workflow="w", first_step="s", max_retries=0)
    assert repo.claim_queued(tid, worker="w1", now="2026-01-01 00:00:00") is True
    # Already RUNNING: a second claim must fail (precondition not met).
    assert repo.claim_queued(tid, worker="w2", now="2026-01-01 00:00:01") is False
    task = repo.get_task(tid)
    assert task["task_status"] == TaskStatus.RUNNING
    assert task["locked_by"] == "w1"


def test_concurrent_claims_only_one_wins(tmp_path):
    # The conditional UPDATE makes claiming atomic: with many threads racing for
    # the same queued task, exactly one wins. (check-then-act could let two win.)
    repo = _repo(tmp_path)
    tid = repo.create_task(input="a", workflow="w", first_step="s", max_retries=0)
    n = 8
    barrier = threading.Barrier(n)
    results: list[bool] = []
    lock = threading.Lock()

    def worker(i: int) -> None:
        barrier.wait()
        won = repo.claim_queued(tid, worker=f"w{i}", now="2026-01-01 00:00:00")
        with lock:
            results.append(won)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1
