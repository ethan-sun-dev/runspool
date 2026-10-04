"""Runner and coordinator behaviour added in 0.2: attempts, notes, degraded runs,
claim tokens, delayed deferrals, and the lifecycle columns steps may not write."""

from __future__ import annotations

from runspool.app import load_context
from runspool.engine.coordinator import Coordinator
from runspool.engine.registry import StepRegistry
from runspool.engine.runner import TaskRunner
from runspool.engine.step import Step, StepContext, StepDeferred, StepResult
from runspool.engine.worker_pool import WorkerPool
from runspool.models import TaskStatus
from tests.support import force_fields


def make_ctx(tmp_path, steps=("a", "b")):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        f"workflows:\n  wf:\n    steps: [{', '.join(steps)}]\n",
        encoding="utf-8",
    )
    return load_context(cfg)


def make_runner(ctx, *steps, notes=None):
    registry = StepRegistry()
    for step in steps:
        registry.register(step)
    return TaskRunner(
        ctx.repo,
        ctx.log,
        ctx.step_runs,
        registry,
        ctx.config,
        notifier=(notes.append if notes is not None else lambda m: None),
    ), registry


def new_task(ctx, *, token="t1", claim=True):
    tid = ctx.repo.create_task(input="in", workflow="wf", first_step="a", max_retries=3)
    if claim:
        ctx.state_machine("wf").claim(tid, worker="w", token=token)
    return tid


class Recorder(Step):
    name = "a"

    def __init__(self, result=None, defer=None):
        self.attempts: list[int] = []
        self._result = result or StepResult()
        self._defer = defer

    def run(self, ctx: StepContext) -> StepResult:
        self.attempts.append(ctx.attempt)
        if self._defer is not None:
            raise self._defer
        return self._result


def test_attempt_counts_every_earlier_run_of_the_step(tmp_path):
    ctx = make_ctx(tmp_path)
    step = Recorder(defer=StepDeferred("not yet"))
    runner, _ = make_runner(ctx, step)
    tid = new_task(ctx, token=None)
    for _ in range(3):
        runner.execute(tid)
        ctx.state_machine("wf").claim(tid, worker="w")
    assert step.attempts == [1, 2, 3]


def test_run_notes_and_degraded_runs_are_recorded(tmp_path):
    ctx = make_ctx(tmp_path, steps=("a",))
    runner, _ = make_runner(ctx, Recorder(StepResult(message="no cover found", degraded=True)))
    tid = new_task(ctx)
    runner.execute(tid, claim_token="t1")
    run = ctx.step_runs.list_for_task(tid)[0]
    assert (run["status"], run["note"]) == ("degraded", "no cover found")
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.PARTIALLY_COMPLETED


def test_a_deferral_note_is_its_reason(tmp_path):
    ctx = make_ctx(tmp_path)
    deferral = StepDeferred("waiting for upload", delay_seconds=60)
    runner, _ = make_runner(ctx, Recorder(defer=deferral))
    tid = new_task(ctx)
    runner.execute(tid, claim_token="t1")
    run = ctx.step_runs.list_for_task(tid)[0]
    assert (run["status"], run["note"]) == ("deferred", "waiting for upload")
    assert ctx.repo.get_task(tid)["next_retry_at"] is not None


def test_a_step_cannot_change_lifecycle_columns_through_updates(tmp_path):
    ctx = make_ctx(tmp_path)
    runner, _ = make_runner(ctx, Recorder(StepResult(updates={"task_status": "completed"})))
    tid = new_task(ctx)
    runner.execute(tid, claim_token="t1")
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.FAILED  # the attempt failed the step
    assert "use the state machine" in task["last_error"]


def test_a_step_may_set_business_columns(tmp_path):
    ctx = make_ctx(tmp_path)
    runner, _ = make_runner(ctx, Recorder(StepResult(updates={"name": "Invoice 42"})))
    tid = new_task(ctx)
    runner.execute(tid, claim_token="t1")
    assert ctx.repo.get_task(tid)["name"] == "Invoice 42"


def test_a_stale_run_request_is_skipped_without_running_the_step(tmp_path):
    ctx = make_ctx(tmp_path)
    notes: list[str] = []
    step = Recorder()
    runner, _ = make_runner(ctx, step, notes=notes)
    tid = new_task(ctx, token="current")
    runner.execute(tid, claim_token="from-an-earlier-claim")
    assert step.attempts == []
    assert ctx.step_runs.list_for_task(tid) == []
    assert "stale" in notes[0]


def test_coordinator_waits_out_a_deferral_delay_and_hands_out_tokens(tmp_path):
    ctx = make_ctx(tmp_path)
    runner, registry = make_runner(ctx, Recorder())
    seen_tokens: list[str | None] = []
    real_execute = runner.execute

    def spy(task_id, *, claim_token=None):
        seen_tokens.append(claim_token)
        real_execute(task_id, claim_token=claim_token)

    runner.execute = spy
    pool = WorkerPool(1)
    coordinator = Coordinator(ctx.repo, ctx.log, registry, runner, pool, ctx.config)
    waiting = new_task(ctx, claim=False)
    force_fields(ctx.repo, waiting, {"next_retry_at": "2999-01-01 00:00:00"})
    try:
        assert coordinator.tick() == 0  # not before its time
        assert ctx.repo.get_task(waiting)["task_status"] == TaskStatus.QUEUED
        ctx.state_machine("wf").wake(waiting)
        assert coordinator.tick() == 1
        pool.drain()
    finally:
        pool.shutdown(wait=True)
    assert len(seen_tokens) == 1 and seen_tokens[0]
    assert ctx.repo.get_task(waiting)["step"] == "b"
