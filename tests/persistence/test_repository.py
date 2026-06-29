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
