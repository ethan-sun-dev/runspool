"""The ``store`` seam contract. Every store implementation must pass it.

Use it from your plugin's tests by subclassing and implementing ``open_store``,
which opens the store kept at ``location`` (an empty directory the first time).
It must be a classmethod or staticmethod on an importable class: the cross-process
test calls it again in other processes to open the *same* store::

    from runspool.testing import StoreContract

    class TestMyStore(StoreContract):
        @classmethod
        def open_store(cls, location):
            return MyStore(location)

The contract covers what the state machine and scheduler rely on: atomic claiming
(exactly one winner, across threads and across processes, which is how the daemon
and a CLI ``run --force`` race), the fields a claim sets, the update allow-list,
the scheduling order of queued tasks, due retries, stale-heartbeat detection,
ordering of events, active-task lookup, and step-run bookkeeping.
"""

from __future__ import annotations

import importlib
import multiprocessing
import threading
from pathlib import Path
from typing import Any

import pytest

from runspool.clock import utcnow_text
from runspool.models import TaskStatus

NOW = "2026-01-01 00:00:00"


def _add(store: Any, input: str = "in", **kw: Any) -> int:
    return store.repo.create_task(input=input, workflow="wf", first_step="a", max_retries=3, **kw)


def _claim_in_another_process(
    module: str, qualname: str, location: str, task_id: int, worker: str
) -> bool:
    cls: Any = importlib.import_module(module)
    for part in qualname.split("."):
        cls = getattr(cls, part)
    return cls.open_store(Path(location)).repo.claim_queued(task_id, worker=worker, now=NOW)


class StoreContract:
    @classmethod
    def open_store(cls, location: Path) -> Any:
        raise NotImplementedError("implement open_store(location) in your contract subclass")

    @pytest.fixture
    def store(self, tmp_path):
        return self.open_store(tmp_path)

    def test_create_then_get(self, store):
        task_id = _add(store, "x.txt", name="X")
        task = store.repo.get_task(task_id)
        assert task["id"] == task_id
        assert (task["input"], task["name"], task["workflow"], task["step"]) == (
            "x.txt",
            "X",
            "wf",
            "a",
        )
        assert task["task_status"] == TaskStatus.QUEUED
        assert store.repo.get_task(task_id + 1000) is None

    def test_ids_increase(self, store):
        assert _add(store, "a") < _add(store, "b")

    def test_claim_is_exclusive_and_only_from_queued(self, store):
        task_id = _add(store)
        assert store.repo.claim_queued(task_id, worker="w1", now=NOW) is True
        assert store.repo.claim_queued(task_id, worker="w2", now=NOW) is False
        task = store.repo.get_task(task_id)
        assert task["task_status"] == TaskStatus.RUNNING
        assert (task["locked_by"], task["locked_at"], task["heartbeat_at"]) == ("w1", NOW, NOW)

    def test_concurrent_claims_have_exactly_one_winner(self, store):
        task_id = _add(store)
        results: list[bool] = []
        barrier = threading.Barrier(8)

        def claim(n: int) -> None:
            barrier.wait()
            results.append(store.repo.claim_queued(task_id, worker=f"w{n}", now=NOW))

        threads = [threading.Thread(target=claim, args=(n,)) for n in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert len(results) == 8, "a claiming thread raised instead of returning"
        assert results.count(True) == 1

    def test_concurrent_claims_across_processes_have_exactly_one_winner(self, tmp_path):
        store = self.open_store(tmp_path)
        task_id = _add(store)
        cls = type(self)
        args = [
            (cls.__module__, cls.__qualname__, str(tmp_path), task_id, f"p{n}") for n in range(4)
        ]
        with multiprocessing.get_context("spawn").Pool(4) as pool:
            results = pool.starmap(_claim_in_another_process, args)
        assert results.count(True) == 1

    def test_update_fields_is_allow_listed(self, store):
        task_id = _add(store)
        store.repo.update_fields(task_id, {"priority": 5, "last_error": "boom"})
        task = store.repo.get_task(task_id)
        assert (task["priority"], task["last_error"]) == (5, "boom")
        for column in ("id", "input"):
            with pytest.raises(ValueError):
                store.repo.update_fields(task_id, {column: "x"})

    def test_listings(self, store):
        first, second = _add(store, "a"), _add(store, "b")
        store.repo.update_fields(second, {"task_status": TaskStatus.PAUSED})
        assert [t["id"] for t in store.repo.list_all()] == [first, second]
        assert [t["id"] for t in store.repo.list_by_status(TaskStatus.QUEUED)] == [first]
        assert [t["id"] for t in store.repo.list_by_status(TaskStatus.PAUSED)] == [second]

    def test_queued_tasks_are_listed_by_priority_then_first_in(self, store):
        first, second, urgent = _add(store, "a"), _add(store, "b"), _add(store, "c")
        store.repo.update_fields(urgent, {"priority": 5})
        queued = [t["id"] for t in store.repo.list_by_status(TaskStatus.QUEUED)]
        assert queued == [urgent, first, second]

    def test_due_failed_respects_next_retry_at(self, store):
        later, due, unset = _add(store, "a"), _add(store, "b"), _add(store, "c")
        for task_id, when in ((later, "2999-01-01 00:00:00"), (due, "2000-01-01 00:00:00")):
            store.repo.update_fields(
                task_id, {"task_status": TaskStatus.FAILED, "next_retry_at": when}
            )
        store.repo.update_fields(unset, {"task_status": TaskStatus.FAILED})
        assert sorted(t["id"] for t in store.repo.list_due_failed()) == [due, unset]

    def test_stale_running_uses_the_heartbeat(self, store):
        stale, fresh = _add(store, "a"), _add(store, "b")
        store.repo.claim_queued(stale, worker="w", now="2000-01-01 00:00:00")
        store.repo.claim_queued(fresh, worker="w", now=utcnow_text())
        assert [t["id"] for t in store.repo.list_stale_running(60)] == [stale]

    def test_find_active_ignores_terminal_tasks(self, store):
        done = _add(store, "same")
        store.repo.update_fields(done, {"task_status": TaskStatus.COMPLETED})
        assert store.repo.find_active_by_input("same") is None
        live = _add(store, "same")
        assert store.repo.find_active_by_input("same")["id"] == live

    def test_events_are_listed_newest_first(self, store):
        from runspool.models import EventType

        task_id = _add(store)
        store.log.add(task_id, EventType.CREATED, step="a", message="one")
        store.log.add(task_id, EventType.CLAIMED, step="a", payload={"k": 1})
        events = store.log.list_for_task(task_id)
        assert [e["message"] for e in events] == [None, "one"]
        assert len(store.log.list_for_task(task_id, limit=1)) == 1

    def test_step_run_bookkeeping(self, store):
        task_id = _add(store)
        run = store.step_runs.start(task_id, "a")
        store.step_runs.finish(run, status="ok", duration_ms=5)
        store.step_runs.start(task_id, "b")
        assert store.step_runs.close_running_for_task(task_id) == 1
        runs = store.step_runs.list_for_task(task_id)
        assert [(r["step"], r["status"]) for r in runs] == [("a", "ok"), ("b", "interrupted")]

    def test_check_succeeds_on_a_healthy_store(self, store):
        assert isinstance(store.check(), str)
