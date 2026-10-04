"""TaskRunner tests with fake steps over a real DB and state machine."""

import time

import pytest

from runspool.app import load_context
from runspool.engine.registry import StepRegistry
from runspool.engine.runner import TaskRunner
from runspool.engine.step import Step, StepContext, StepDeferred, StepResult
from runspool.models import TaskStatus
from runspool.persistence.state_machine import StateMachine
from tests.conftest import write_config
from tests.support import force_fields


class _Ok(Step):
    name = "alpha"

    def run(self, ctx: StepContext) -> StepResult:
        return StepResult(message="done")


class _Boom(Step):
    name = "alpha"

    def run(self, ctx: StepContext) -> StepResult:
        raise RuntimeError("kaboom")


class _Defer(Step):
    name = "alpha"

    def run(self, ctx: StepContext) -> StepResult:
        raise StepDeferred()


class _Midway(Step):
    """A step during whose run the user acts on the task (pause, terminate...)."""

    name = "alpha"

    def __init__(self, act):
        self.act = act
        self.runner = None
        self.saw_stop = None

    def run(self, ctx: StepContext) -> StepResult:
        tid = ctx.task["id"]
        self.act(_machine(self.runner, tid), tid)
        self.saw_stop = ctx.should_stop()
        ctx.heartbeat()
        return StepResult(message="done")


def _machine(runner, tid):
    task = runner.repo.get_task(tid)
    return StateMachine(runner.repo, runner.log, workflow=runner.config.workflow(task["workflow"]))


class _BadUpdates(Step):
    name = "alpha"

    def run(self, ctx: StepContext) -> StepResult:
        return StepResult(updates={"not_a_column": "x"})


def _setup(tmp_path, step, *, steps=("alpha", "beta"), notifier=None):
    cfg = write_config(tmp_path, steps=steps)
    ctx = load_context(cfg)
    reg = StepRegistry()
    reg.register(step)
    kwargs = {"notifier": notifier} if notifier is not None else {}
    runner = TaskRunner(ctx.repo, ctx.log, ctx.step_runs, reg, ctx.config, **kwargs)
    tid = ctx.repo.create_task(input="x", workflow="local_file", first_step="alpha", max_retries=2)
    sm = StateMachine(ctx.repo, ctx.log, workflow=ctx.config.workflow("local_file"))
    sm.claim(tid, worker="w1")
    if isinstance(step, _Midway):
        step.runner = runner
    return runner, ctx.repo, ctx.step_runs, tid


def test_success_advances_and_records_run(tmp_path):
    runner, repo, runs, tid = _setup(tmp_path, _Ok())
    runner.execute(tid)
    task = repo.get_task(tid)
    assert task["step"] == "beta"
    assert task["task_status"] == TaskStatus.QUEUED
    assert runs.list_for_task(tid)[0]["status"] == "ok"


def test_failure_marks_failed_and_records_error(tmp_path):
    runner, repo, runs, tid = _setup(tmp_path, _Boom())
    runner.execute(tid)
    task = repo.get_task(tid)
    assert task["task_status"] == TaskStatus.FAILED
    assert "kaboom" in task["last_error"]
    assert runs.list_for_task(tid)[0]["status"] == "failed"


def test_defer_keeps_step(tmp_path):
    runner, repo, runs, tid = _setup(tmp_path, _Defer())
    runner.execute(tid)
    task = repo.get_task(tid)
    assert task["step"] == "alpha"
    assert task["task_status"] == TaskStatus.QUEUED
    assert runs.list_for_task(tid)[0]["status"] == "deferred"


def test_terminate_flag_applied_after_step(tmp_path):
    step = _Midway(lambda sm, tid: sm.request_terminate(tid))
    runner, repo, runs, tid = _setup(tmp_path, step)
    runner.execute(tid)
    assert step.saw_stop is True  # the step could see the request and stop early
    assert repo.get_task(tid)["task_status"] == TaskStatus.TERMINATED


def test_terminate_wins_over_concurrent_pause_request(tmp_path):
    # Regression: a task paused mid-step and then terminated must end TERMINATED,
    # not PAUSED: terminate wins at the step boundary.
    def pause_then_terminate(sm, tid):
        sm.request_pause(tid)
        sm.request_terminate(tid)

    runner, repo, runs, tid = _setup(tmp_path, _Midway(pause_then_terminate))
    runner.execute(tid)
    task = repo.get_task(tid)
    assert task["task_status"] == TaskStatus.TERMINATED
    assert task["step"] == "alpha"  # not advanced by the pause


def test_pause_advances_then_pauses_so_step_is_not_rerun(tmp_path):
    # Pause requested mid-step: the step finishes, then the task pauses at the
    # NEXT step. Resuming must not re-run the already-completed step.
    step = _Midway(lambda sm, tid: sm.request_pause(tid))
    runner, repo, runs, tid = _setup(tmp_path, step)  # workflow: alpha -> beta
    runner.execute(tid)
    task = repo.get_task(tid)
    assert task["task_status"] == TaskStatus.PAUSED
    assert task["step"] == "beta"          # advanced past the completed step
    assert task["pause_requested"] == 0    # request consumed
    # alpha ran exactly once and recorded a successful run.
    alpha_runs = [r for r in runs.list_for_task(tid) if r["step"] == "alpha"]
    assert len(alpha_runs) == 1 and alpha_runs[0]["status"] == "ok"


def test_pause_on_last_step_completes_instead_of_pausing(tmp_path):
    # There is no later step to pause before, so the workflow completes.
    step = _Midway(lambda sm, tid: sm.request_pause(tid))
    runner, repo, runs, tid = _setup(tmp_path, step, steps=("alpha",))
    runner.execute(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.COMPLETED


@pytest.mark.parametrize("request_", ["pause", "terminate"])
def test_a_request_made_before_the_step_starts_skips_the_step(tmp_path, request_):
    # Requested while the job waited in the pool: the step does not run at all;
    # a pause pauses in place, a terminate terminates.
    runner, repo, runs, tid = _setup(tmp_path, _Ok())
    sm = _machine(runner, tid)
    sm.request_pause(tid) if request_ == "pause" else sm.request_terminate(tid)
    runner.execute(tid)
    task = repo.get_task(tid)
    expected = TaskStatus.PAUSED if request_ == "pause" else TaskStatus.TERMINATED
    assert (task["task_status"], task["step"]) == (expected, "alpha")
    assert runs.list_for_task(tid) == []


def test_bad_updates_fail_task(tmp_path):
    runner, repo, runs, tid = _setup(tmp_path, _BadUpdates())
    runner.execute(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.FAILED
    run = runs.list_for_task(tid)[0]
    assert run["status"] == "failed"
    assert run["finished_at"] is not None


def test_runner_heartbeats_during_step_without_step_cooperation(tmp_path):
    # A step that never calls ctx.heartbeat() must still have its heartbeat_at
    # refreshed by the runner, so a long step is not reclaimed as stale and
    # executed a second time.
    cfg = write_config(tmp_path, steps=("alpha", "beta"))
    ctx = load_context(cfg)
    observed: dict[str, bool] = {}

    captured = {}

    class _Slow(Step):
        name = "alpha"

        def run(self, c: StepContext) -> StepResult:
            tid = captured["tid"]
            start = ctx.repo.get_task(tid)["heartbeat_at"]
            for _ in range(300):
                if ctx.repo.get_task(tid)["heartbeat_at"] != start:
                    observed["beat"] = True
                    break
                time.sleep(0.01)
            return StepResult(message="ok")

    reg = StepRegistry()
    reg.register(_Slow())
    runner = TaskRunner(
        ctx.repo, ctx.log, ctx.step_runs, reg, ctx.config,
        notifier=lambda m: None, heartbeat_interval=0.02,
    )
    tid = ctx.repo.create_task(input="x", workflow="local_file", first_step="alpha", max_retries=0)
    captured["tid"] = tid
    ctx.state_machine("local_file").claim(tid, worker="w1")  # the runner executes claimed tasks
    force_fields(ctx.repo, tid, {"heartbeat_at": "2000-01-01 00:00:00"})
    runner.execute(tid)
    assert observed.get("beat") is True


def test_failure_notifies_console(tmp_path):
    seen: list[str] = []
    runner, repo, runs, tid = _setup(tmp_path, _Boom(), notifier=seen.append)
    runner.execute(tid)
    assert len(seen) == 1
    assert f"#{tid}" in seen[0] and "kaboom" in seen[0]
