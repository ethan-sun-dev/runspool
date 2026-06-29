"""Runtime wiring: assemble the registry, worker pool, coordinator, and daemon;
provide the one-shot `run` driver and daemon process helpers."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runspool.app import AppContext
from runspool.daemon import Daemon, read_pid
from runspool.engine.coordinator import Coordinator
from runspool.engine.runner import TaskRunner
from runspool.engine.worker_pool import WorkerPool
from runspool.persistence.state_machine import StateMachine
from runspool.registry_builder import build_registry


def _build_coordinator(
    ctx: AppContext, *, notifier: Callable[[str], None] | None = None
) -> Coordinator:
    registry = build_registry(ctx.config)
    runner_kwargs = {"notifier": notifier} if notifier is not None else {}
    runner = TaskRunner(ctx.repo, ctx.log, ctx.step_runs, registry, ctx.config, **runner_kwargs)
    pool = WorkerPool(ctx.config.worker_pool.size)
    return Coordinator(ctx.repo, ctx.log, registry, runner, pool, ctx.config)


def build_daemon(ctx: AppContext) -> Daemon:
    return Daemon(_build_coordinator(ctx), ctx.config)


def _recover(ctx: AppContext) -> None:
    any_workflow = next(iter(ctx.config.workflows))
    sm = StateMachine(
        ctx.repo,
        ctx.log,
        workflow=ctx.config.workflow(any_workflow),
        step_runs=ctx.step_runs,
    )
    sm.recover_interrupted()


def run_until_idle(
    ctx: AppContext,
    *,
    notifier: Callable[[str], None] | None = None,
    max_rounds: int | None = None,
) -> int:
    """Advance every runnable task until no further progress is made.

    Each round claims runnable steps, waits for them to finish, then re-evaluates
    so multi-step tasks flow through to completion. Returns the number of rounds
    executed. Intended for one-shot use; long-running or deferred work belongs to
    the daemon. A safety cap prevents an unbounded loop if steps keep deferring.
    """
    coordinator = _build_coordinator(ctx, notifier=notifier)
    _recover(ctx)
    total_tasks = len(ctx.repo.list_all())
    max_steps = max((len(wf.steps) for wf in ctx.config.workflows.values()), default=1)
    cap = max_rounds if max_rounds is not None else total_tasks * max_steps + 50
    rounds = 0
    try:
        while rounds < cap:
            submitted = coordinator.tick()
            coordinator.pool.drain()
            rounds += 1
            if submitted == 0:
                break
    finally:
        coordinator.pool.shutdown(wait=True)
    return rounds


def daemon_pid_file(ctx: AppContext) -> Path:
    return Path(ctx.config.runtime_dir) / "daemon.pid"


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def daemon_status(ctx: AppContext) -> dict[str, Any]:
    pid = read_pid(daemon_pid_file(ctx))
    if pid is not None and _pid_alive(pid):
        return {"running": True, "pid": pid}
    return {"running": False, "pid": None}


def request_daemon_stop(ctx: AppContext) -> bool:
    import signal

    status = daemon_status(ctx)
    if not status["running"]:
        return False
    os.kill(status["pid"], signal.SIGTERM)
    return True
