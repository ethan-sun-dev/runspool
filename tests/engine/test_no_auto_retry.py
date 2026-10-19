"""A step can opt out of automatic retries (``retry = False``, issue #25).

A step with external side effects must not be run again automatically after it
raised: the side effect may already have happened. With ``retry = False`` the
task goes straight to ``manual_required`` on the first failure, whatever its
remaining retry budget; a person can still requeue it with ``retry``.
"""

from __future__ import annotations

import sys

import pytest

from runspool import commands
from runspool.app import load_context
from runspool.doctor import run_doctor
from runspool.models import TaskStatus
from runspool.runtime import run_until_idle

STEPS = '''
from runspool.engine.step import Step, StepDeferred, StepResult

RUNS = []


class NoRetry(Step):
    name = "no_retry"
    retry = False

    def run(self, ctx):
        RUNS.append(self.name)
        raise RuntimeError("remote call may have happened")


class Retries(Step):
    name = "retries"

    def run(self, ctx):
        RUNS.append(self.name)
        raise RuntimeError("transient")


class RetriesExplicit(Step):
    name = "retries_explicit"
    retry = True

    def run(self, ctx):
        RUNS.append(self.name)
        raise RuntimeError("transient")


class NoRetryDefers(Step):
    name = "no_retry_defers"
    retry = False

    def run(self, ctx):
        RUNS.append(self.name)
        raise StepDeferred("not ready", delay_seconds=3600)
'''


@pytest.fixture
def make(tmp_path, monkeypatch):
    (tmp_path / "retry_steps.py").write_text(STEPS, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "retry_steps", raising=False)

    def make(step: str):
        path = tmp_path / "config.yaml"
        names = ("no_retry", "retries", "retries_explicit", "no_retry_defers")
        classes = ("NoRetry", "Retries", "RetriesExplicit", "NoRetryDefers")
        imports = "".join(
            f"  {n}:\n    import: 'retry_steps:{c}'\n" for n, c in zip(names, classes, strict=True)
        )
        path.write_text(
            f"workspace_root: {tmp_path / 'ws'}\n"
            "scheduler:\n  max_retries: 3\n"
            f"steps:\n{imports}"
            f"workflows:\n  w:\n    steps: [{step}]\n",
            encoding="utf-8",
        )
        ctx = load_context(path)
        tid = ctx.service("tasks").add("x", workflow="w")
        return ctx, tid, sys.modules.get("retry_steps")

    return make


def _runs(step: str) -> int:
    return sys.modules["retry_steps"].RUNS.count(step)


def _run(ctx) -> None:
    run_until_idle(ctx, notifier=lambda m: None)


def test_a_no_retry_step_runs_once_and_needs_a_person(make):
    ctx, tid, _ = make("no_retry")
    _run(ctx)
    task = ctx.repo.get_task(tid)
    assert task["max_retries"] == 3
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert _runs("no_retry") == 1  # run exactly once, with retries left
    assert "remote call may have happened" in task["last_error"]
    assert task["next_retry_at"] is None


@pytest.mark.parametrize("step", ["retries", "retries_explicit"])
def test_the_default_keeps_retrying(make, step):
    ctx, tid, _ = make(step)
    _run(ctx)
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert _runs(step) == 4  # first attempt + 3 retries, as today


def test_a_manual_retry_requeues_it_once(make):
    ctx, tid, _ = make("no_retry")
    _run(ctx)
    commands.retry_task(ctx, tid)
    _run(ctx)
    assert _runs("no_retry") == 2
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.MANUAL_REQUIRED


def test_a_deferral_is_still_not_a_failure(make):
    ctx, tid, _ = make("no_retry_defers")
    _run(ctx)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.QUEUED and task["next_retry_at"] is not None
    assert task["retry_count"] == 0
    assert _runs("no_retry_defers") == 1


def test_the_lazy_proxy_forwards_the_flag(make):
    ctx, _tid, _ = make("no_retry")
    step = ctx.service("steps").registry.get("no_retry")
    assert step.retry is False  # not the proxy's default
    assert ctx.service("steps").registry.get("retries").retry is True


def test_doctor_lists_steps_that_are_never_retried(make):
    ctx, _tid, _ = make("no_retry")
    checks = {c.name: c for c in run_doctor(ctx)}
    check = checks["no_auto_retry"]
    assert check.ok and "no_retry" in check.detail and "no_retry_defers" in check.detail
    assert "retries" not in check.detail.replace("no_retry", "")
