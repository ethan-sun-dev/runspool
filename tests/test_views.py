from runspool.commands import add_task
from runspool.models import TaskStatus
from runspool.views import inspect_view, task_view


def test_task_view_has_stable_fields(ctx):
    tid = add_task(ctx, "in.txt", workflow="local_file", name="N")
    view = task_view(ctx.repo.get_task(tid))
    assert view["id"] == tid
    assert view["name"] == "N"
    assert view["task_status"] == TaskStatus.QUEUED
    assert set(view) >= {"input", "workflow", "step", "priority", "created_at"}


def test_inspect_actions_for_manual_required(ctx):
    tid = add_task(ctx, "in.txt", workflow="local_file")
    ctx.repo.update_fields(
        tid, {"task_status": TaskStatus.MANUAL_REQUIRED, "last_error": "missing source"}
    )
    view = inspect_view(ctx, ctx.repo.get_task(tid))
    assert view["status"] == TaskStatus.MANUAL_REQUIRED
    assert "retry" in view["available_actions"]
    assert "set-step" in view["available_actions"]
    assert "missing source" in view["suggested_next_action"]


def test_inspect_actions_for_completed_is_empty(ctx):
    tid = add_task(ctx, "in.txt", workflow="local_file")
    ctx.repo.update_fields(tid, {"task_status": TaskStatus.COMPLETED})
    view = inspect_view(ctx, ctx.repo.get_task(tid))
    assert view["available_actions"] == []
    assert "complete" in view["suggested_next_action"].lower()


def test_inspect_actions_for_paused(ctx):
    tid = add_task(ctx, "in.txt", workflow="local_file")
    ctx.repo.update_fields(tid, {"task_status": TaskStatus.PAUSED})
    view = inspect_view(ctx, ctx.repo.get_task(tid))
    assert "resume" in view["available_actions"]
