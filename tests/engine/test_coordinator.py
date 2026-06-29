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


class _Boom(Step):
    name = "alpha"

    def run(self, ctx: StepContext) -> StepResult:
        raise RuntimeError("boom")


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


def test_auto_retry_consumes_budget_to_manual_required(tmp_path):
    # With retry_delay_seconds defaulting to 0, repeated ticks should requeue the
    # FAILED task and consume max_retries until it lands in manual_required.
    ctx, coord, pool = _coordinator(tmp_path, _Boom())
    tid = ctx.repo.create_task(
        input="x", workflow="local_file", first_step="alpha", max_retries=2
    )
    for _ in range(20):
        submitted = coord.tick()
        pool.drain()
        if submitted == 0:
            break
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert task["retry_count"] == 3  # initial attempt + 2 retries
    pool.shutdown()


def test_pause_pending_counts_toward_quota(tmp_path):
    # A PAUSE_PENDING task is still being executed by a worker thread, so its
    # per-step quota slot is occupied. With quota 1 the coordinator must not
    # claim a second QUEUED task of the same step.
    ctx, coord, pool = _coordinator(tmp_path, _Ok())
    busy = ctx.repo.create_task(
        input="busy", workflow="local_file", first_step="alpha", max_retries=0
    )
    ctx.repo.update_fields(busy, {"task_status": TaskStatus.PAUSE_PENDING})
    queued = ctx.repo.create_task(
        input="q", workflow="local_file", first_step="alpha", max_retries=0
    )

    submitted = coord.tick()
    pool.drain()
    assert submitted == 0
    assert ctx.repo.get_task(queued)["task_status"] == TaskStatus.QUEUED
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
