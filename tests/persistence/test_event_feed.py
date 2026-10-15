"""EventLog.since: the cursor-based event feed."""

from runspool.persistence.connection import Database
from runspool.persistence.event_log import EventLog
from runspool.persistence.repository import TaskRepository


def _log(tmp_path):
    db = Database(tmp_path / "runspool.db")
    db.init()
    return TaskRepository(db), EventLog(db)


def test_since_returns_newer_events_oldest_first(tmp_path):
    repo, log = _log(tmp_path)
    a = repo.create_task(input="a", workflow="w", first_step="s", max_retries=0)
    b = repo.create_task(input="b", workflow="w", first_step="s", max_retries=0)
    for task_id, msg in ((a, "one"), (b, "two"), (a, "three")):
        log.add(task_id, "created", message=msg)

    everything = log.since(0)
    assert [e["id"] for e in everything] == sorted(e["id"] for e in everything)
    cursor = everything[-2]["id"]
    assert [e["message"] for e in log.since(cursor)] == [everything[-1]["message"]]
    assert all(e["task_id"] == a for e in log.since(0, task_id=a))
    assert len(log.since(0, limit=1)) == 1
