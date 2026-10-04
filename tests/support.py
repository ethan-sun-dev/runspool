"""Test helpers."""

from runspool.models import TaskStatus


def force_fields(repo, task_id: int, fields: dict) -> None:
    """Set lifecycle columns directly, whatever the task's status. Test setup only:
    production code changes them through the state machine."""
    assert repo.transition(task_id, fields, expect=tuple(TaskStatus))
