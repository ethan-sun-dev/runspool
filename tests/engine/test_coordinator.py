from runspool.app import load_context
from runspool.engine.coordinator import Coordinator
from runspool.engine.registry import StepRegistry
from runspool.engine.runner import TaskRunner
from runspool.engine.step import Step, StepContext, StepResult
from runspool.engine.worker_pool import WorkerPool
from runspool.models import TaskStatus
from tests.conftest import write_config


class _Ok(Step):
    name = "alpha"

    def run(self, ctx: StepContext) -> StepResult:
        return StepResult(message="ok")


class _Skip(Step):
    name = "alpha"

    def when(self, task, config) -> bool:
        return False

    def run(self, ctx: StepContext) -> StepResult:  # pragma: no cover - never runs
        raise AssertionError("should be skipped")


def _coordinator(tmp_path, step, *, steps=("alpha", "beta")):
    cfg = write_config(tmp_path, steps=steps)
    ctx = load_context(cfg)
    reg = StepRegistry()
    reg.register(step)
    runner = TaskRunner(ctx.repo, ctx.log, ctx.step_runs, reg, ctx.config, notifier=lambda m: None)
    pool = WorkerPool(2)
    coord = Coordinator(ctx.repo, ctx.log, reg, runner, pool, ctx.config)
    return ctx, coord, pool


def test_tick_claims_and_runs(tmp_path):
    ctx, coord, pool = _coordinator(tmp_path, _Ok())
    tid = ctx.repo.create_task(
        input="x", workflow="local_file", first_step="alpha", max_retries=0
    )
    submitted = coord.tick()
    pool.drain()
    assert submitted == 1
    assert ctx.repo.get_task(tid)["step"] == "beta"
    pool.shutdown()


def test_when_false_skips_step(tmp_path):
    ctx, coord, pool = _coordinator(tmp_path, _Skip())
    tid = ctx.repo.create_task(
        input="x", workflow="local_file", first_step="alpha", max_retries=0
    )
    coord.tick()
    pool.drain()
    assert ctx.repo.get_task(tid)["step"] == "beta"
    pool.shutdown()


def test_tick_returns_zero_when_idle(tmp_path):
    ctx, coord, pool = _coordinator(tmp_path, _Ok())
    assert coord.tick() == 0
    pool.shutdown()


def test_unregistered_step_does_not_crash_tick(tmp_path):
    ctx, coord, pool = _coordinator(tmp_path, _Ok())
    # first_step that the registry does not know about
    tid = ctx.repo.create_task(
        input="x", workflow="local_file", first_step="beta", max_retries=0
    )
    assert coord.tick() == 0  # skipped, no exception
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.QUEUED
    pool.shutdown()
