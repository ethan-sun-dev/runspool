"""The ``store`` seam contract. Every store implementation must pass it.

Use it from your plugin's tests by subclassing and providing a ``store`` fixture
that returns a fresh, empty store::

    from runspool.testing import StoreContract

    class TestMyStore(StoreContract):
        @pytest.fixture
        def store(self, tmp_path):
            return MyStore(tmp_path)

The contract covers what the state machine and scheduler rely on: atomic claiming
(exactly one winner, even across threads), the update allow-list, ordering of
listings and events, active-task lookup, and step-run bookkeeping.
"""

from __future__ import annotations

import threading
from typing import Any

from runspool.models import TaskStatus

NOW = "2026-01-01 00:00:00"


def _add(store: Any, input: str = "in", **kw: Any) -> int:
    return store.repo.create_task(input=input, workflow="wf", first_step="a", max_retries=3, **kw)


class StoreContract:
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
        assert task["locked_by"] == "w1"

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
        assert results.count(True) == 1

    def test_update_fields_is_allow_listed(self, store):
        import pytest

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
