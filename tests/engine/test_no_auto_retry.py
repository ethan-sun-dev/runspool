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

STEPS = """
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
"""


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


# -- Interrupted runs (review): a worker crash or heartbeat timeout must not rerun
# a step that opted out of automatic retries either --------------------------------

CRASHING = """
import os
from pathlib import Path

from runspool.engine.step import Step, StepResult

MARKER = Path({marker!r})


def _ran_before(name):
    lines = MARKER.read_text().splitlines() if MARKER.exists() else []
    MARKER.write_text("\\n".join([*lines, name]) + "\\n")
    return name in lines


class CrashNoRetry(Step):
    name = "crash_no_retry"
    retry = False

    def run(self, ctx):
        if not _ran_before(self.name):
            os._exit(3)  # the side effect happened; the process dies before recording it
        return StepResult(message="ran again")


class CrashRetries(Step):
    name = "crash_retries"

    def run(self, ctx):
        if not _ran_before(self.name):
            os._exit(3)
        return StepResult(message="ran again")
"""

_RUN = """
import sys
sys.path.insert(0, sys.argv[2])
from runspool.app import load_context
from runspool.runtime import run_until_idle
run_until_idle(load_context(sys.argv[1]), notifier=lambda m: None)
"""


@pytest.fixture
def crashing(tmp_path, monkeypatch):
    import os
    import subprocess

    marker = tmp_path / "runs.txt"
    module = tmp_path / "crash_steps.py"
    module.write_text(CRASHING.format(marker=str(marker)), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "crash_steps", raising=False)

    def make(step: str):
        path = tmp_path / f"{step}.yaml"
        path.write_text(
            f"workspace_root: {tmp_path / ('ws-' + step)}\n"
            "scheduler:\n  max_retries: 3\n"
            "steps:\n"
            "  crash_no_retry:\n    import: 'crash_steps:CrashNoRetry'\n"
            "  crash_retries:\n    import: 'crash_steps:CrashRetries'\n"
            f"workflows:\n  w:\n    steps: [{step}]\n",
            encoding="utf-8",
        )
        ctx = load_context(path)
        tid = ctx.service("tasks").add("x", workflow="w")
        env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
        out = subprocess.run(
            [sys.executable, "-c", _RUN, str(path), str(tmp_path)],
            capture_output=True, text=True, timeout=120, env=env,
        )  # fmt: skip
        assert out.returncode == 3, out.stdout + out.stderr  # the worker process died
        return load_context(path), tid

    def runs(step):
        return marker.read_text().splitlines().count(step) if marker.exists() else 0

    return make, runs


def test_a_crash_inside_a_no_retry_step_is_not_rerun_on_recovery(crashing):
    make, runs = crashing
    ctx, tid = make("crash_no_retry")
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.RUNNING  # left behind
    _run(ctx)  # recovery runs first, then scheduling
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "interrupted" in task["last_error"]
    assert runs("crash_no_retry") == 1  # not run again


def test_a_manual_retry_after_a_crash_still_requeues(crashing):
    make, runs = crashing
    ctx, tid = make("crash_no_retry")
    _run(ctx)
    commands.retry_task(ctx, tid)
    _run(ctx)
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.COMPLETED
    assert runs("crash_no_retry") == 2


def test_a_crash_inside_an_ordinary_step_is_still_requeued(crashing):
    make, runs = crashing
    ctx, tid = make("crash_retries")
    _run(ctx)
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.COMPLETED  # as on main
    assert runs("crash_retries") == 2


def _stale_running(tmp_path, step, *, registered=True):
    from runspool.engine.coordinator import Coordinator
    from runspool.engine.registry import StepRegistry
    from runspool.engine.runner import TaskRunner
    from runspool.engine.worker_pool import WorkerPool
    from tests.conftest import write_config
    from tests.support import force_fields

    ctx = load_context(write_config(tmp_path, steps=(step.name,)))
    reg = StepRegistry()
    if registered:
        reg.register(step)
    runner = TaskRunner(ctx.repo, ctx.log, ctx.step_runs, reg, ctx.config, notifier=lambda m: None)
    coord = Coordinator(ctx.repo, ctx.log, reg, runner, WorkerPool(1), ctx.config)
    tid = ctx.repo.create_task(input="x", workflow="local_file", first_step=step.name,
                               max_retries=3)  # fmt: skip
    force_fields(ctx.repo, tid, {"task_status": TaskStatus.RUNNING, "claim_token": "gone",
                                 "heartbeat_at": "2000-01-01 00:00:00"})  # fmt: skip
    coord.reclaim_stale(60)
    return ctx.repo.get_task(tid)


def test_a_heartbeat_timeout_does_not_requeue_a_no_retry_step(tmp_path):
    from runspool.engine.step import Step, StepResult

    class Remote(Step):
        name = "remote"
        retry = False

        def run(self, ctx):
            return StepResult()

    task = _stale_running(tmp_path, Remote())
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "heartbeat timeout" in task["last_error"]


def test_a_heartbeat_timeout_still_requeues_an_ordinary_step(tmp_path):
    from runspool.engine.step import Step, StepResult

    class Local(Step):
        name = "local"

        def run(self, ctx):
            return StepResult()

    assert _stale_running(tmp_path, Local())["task_status"] == TaskStatus.QUEUED


# -- retry must be a bool (review P3) -------------------------------------------------


@pytest.mark.parametrize("value", [0, None, "no"])
def test_a_non_bool_retry_is_refused_at_registration(value):
    from runspool.engine.registry import StepRegistry
    from runspool.engine.step import Step, StepResult

    class Odd(Step):
        name = "odd"
        retry = value

        def run(self, ctx):
            return StepResult()

    with pytest.raises(TypeError, match="retry"):
        StepRegistry().register(Odd())


def test_a_lazily_loaded_step_with_a_non_bool_retry_does_not_load(tmp_path, monkeypatch):
    (tmp_path / "odd_steps.py").write_text(
        "from runspool.engine.step import Step, StepResult\n"
        "class Odd(Step):\n    name = 'odd'\n    retry = 0\n"
        "    def run(self, ctx):\n        return StepResult()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "odd_steps", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        "steps:\n  odd:\n    import: 'odd_steps:Odd'\n"
        "workflows:\n  w:\n    steps: [odd]\n",
        encoding="utf-8",
    )
    ctx = load_context(path)
    checks = {c.name: c for c in run_doctor(ctx)}
    assert not checks["steps"].ok and "retry" in checks["steps"].detail


def test_the_state_machine_fail_api_takes_the_flag(make):
    from runspool.persistence.state_machine import StateMachine

    ctx, tid, _ = make("retries")
    sm = StateMachine(ctx.repo, ctx.log, workflow=ctx.config.workflow("w"))
    sm.claim(tid, worker="w1")
    sm.fail(tid, "boom", no_retry=True)
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.MANUAL_REQUIRED


# -- Second review (P3): daemon start-up recovery, the external-step failure path, and
# steps the registry cannot answer for -------------------------------------------------


def test_daemon_start_up_recovery_does_not_requeue_a_no_retry_step(crashing):
    from runspool.runtime import build_daemon

    make, runs = crashing
    ctx, tid = make("crash_no_retry")
    build_daemon(ctx).recover()  # what the daemon does first when it starts
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "interrupted" in task["last_error"]
    assert runs("crash_no_retry") == 1


def test_daemon_start_up_recovery_still_requeues_an_ordinary_step(crashing):
    from runspool.runtime import build_daemon

    make, _runs_of = crashing
    ctx, tid = make("crash_retries")
    build_daemon(ctx).recover()
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.QUEUED  # as on main


def _external_without_work_items(tmp_path, retry):
    from runspool.engine.external import ExternalStep
    from runspool.engine.registry import StepRegistry
    from runspool.engine.runner import TaskRunner
    from runspool.engine.step import StepResult

    class Ask(ExternalStep):
        name = "ask"

        def validate(self, ctx, submission):
            return StepResult()

    Ask.retry = retry
    cfg = tmp_path / "runspool.yaml"
    cfg.write_text(
        f"workspace_root: {tmp_path / 'ws'}\nscheduler:\n  max_retries: 3\n"
        "workflows:\n  w:\n    steps: [ask]\n",
        encoding="utf-8",
    )
    ctx = load_context(cfg)
    reg = StepRegistry()
    reg.register(Ask())
    tid = ctx.repo.create_task(input="x", workflow="w", first_step="ask", max_retries=3)
    ctx.state_machine("w").claim(tid, worker="t")
    runner = TaskRunner(ctx.repo, ctx.log, ctx.step_runs, reg, ctx.config, notifier=lambda m: None)
    runner.execute(tid)
    return ctx.repo.get_task(tid)


def test_an_external_no_retry_step_that_cannot_open_a_work_item_needs_a_person(tmp_path):
    task = _external_without_work_items(tmp_path, retry=False)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "has no work items" in task["last_error"]


def test_an_ordinary_external_step_that_cannot_open_a_work_item_is_retried(tmp_path):
    task = _external_without_work_items(tmp_path, retry=True)
    assert task["task_status"] != TaskStatus.MANUAL_REQUIRED  # retries left: as on main
    assert task["retry_count"] == 1


def test_an_unknown_or_unloadable_step_is_treated_as_not_retried():
    from runspool.core.steps import LazyStep
    from runspool.engine.registry import StepRegistry

    reg = StepRegistry()
    assert reg.no_retry("missing") is True

    def broken():
        raise ImportError("the step's module cannot be imported")

    reg.register(LazyStep("broken", broken))
    assert reg.no_retry("broken") is True


def test_a_heartbeat_timeout_on_an_unregistered_step_needs_a_person(tmp_path):
    from runspool.engine.step import Step, StepResult

    class Gone(Step):
        name = "gone"

        def run(self, ctx):
            return StepResult()

    task = _stale_running(tmp_path, Gone(), registered=False)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "heartbeat timeout" in task["last_error"]
