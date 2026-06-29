"""TaskRunner tests with fake steps over a real DB and state machine."""

from runspool.app import load_context
from runspool.engine.registry import StepRegistry
from runspool.engine.runner import TaskRunner
from runspool.engine.step import Step, StepContext, StepDeferred, StepResult
from runspool.models import TaskStatus
from runspool.persistence.state_machine import StateMachine
from tests.conftest import write_config


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


class _Stoppable(Step):
    name = "alpha"

    def run(self, ctx: StepContext) -> StepResult:
        assert ctx.should_stop() is True
        ctx.heartbeat()
        return StepResult(message="returned after stop")


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
    runner, repo, runs, tid = _setup(tmp_path, _Stoppable())
    repo.update_fields(tid, {"terminate_requested": 1})
    runner.execute(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.TERMINATED


def test_pause_advances_then_pauses_so_step_is_not_rerun(tmp_path):
    # Pause requested mid-step: the step finishes, then the task pauses at the
    # NEXT step. Resuming must not re-run the already-completed step.
    runner, repo, runs, tid = _setup(tmp_path, _Ok())  # workflow: alpha -> beta
    repo.update_fields(tid, {"pause_requested": 1})
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
    runner, repo, runs, tid = _setup(tmp_path, _Ok(), steps=("alpha",))
    repo.update_fields(tid, {"pause_requested": 1})
    runner.execute(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.COMPLETED


def test_bad_updates_fail_task(tmp_path):
    runner, repo, runs, tid = _setup(tmp_path, _BadUpdates())
    runner.execute(tid)
    assert repo.get_task(tid)["task_status"] == TaskStatus.FAILED
    run = runs.list_for_task(tid)[0]
    assert run["status"] == "failed"
    assert run["finished_at"] is not None


def test_failure_notifies_console(tmp_path):
    seen: list[str] = []
    runner, repo, runs, tid = _setup(tmp_path, _Boom(), notifier=seen.append)
    runner.execute(tid)
    assert len(seen) == 1
    assert f"#{tid}" in seen[0] and "kaboom" in seen[0]
