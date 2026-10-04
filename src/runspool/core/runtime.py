"""``runtime``: runner, scheduler (coordinator + worker pool) and daemon wiring."""

from __future__ import annotations

import os
import signal
from collections.abc import Callable
from pathlib import Path
from typing import Any

from runspool.config import AppConfig
from runspool.core.store import Store
from runspool.daemon import Daemon, read_pid
from runspool.engine.coordinator import Coordinator
from runspool.engine.gate import DECISIONS, Allow, Ask, Decision, Defer, Deny, GateRequest
from runspool.engine.runner import TaskRunner
from runspool.engine.worker_pool import WorkerPool
from runspool.kernel import Plugin, StartupError
from runspool.persistence.state_machine import StateMachine


class Gate:
    """The pre-execute gate (see runspool.engine.gate), in this order:

    1. plugins' ``step/pre-execute`` waterfall listeners decide (default: allow);
       anything that is not a decision, or an exception, refuses the step;
    2. a step with side effects, or an ``Ask``, needs approval unless this exact
       attempt was approved; without an active approval service, or with policy
       ``never``, the step is refused instead;
    3. ``steps.guard`` checks may still refuse it.

    Step 2 is RunSpool's own rule, applied after the plugins: no listener can wave a
    step with side effects through.
    """

    def __init__(self, ctx: Any, steps: Any) -> None:
        self._ctx = ctx
        self._steps = steps

    def __call__(self, request: GateRequest) -> Decision:
        try:
            decision = self._ctx.waterfall("step/pre-execute", request, default=Allow)
        except Exception as exc:  # noqa: BLE001 - a broken policy fails closed
            return Deny(f"pre-execute policy failed: {type(exc).__name__}: {exc}")
        if not isinstance(decision, DECISIONS):
            return Deny(f"pre-execute policy returned {decision!r}, not a decision")
        if isinstance(decision, (Deny, Defer)):
            return decision
        needs_approval = isinstance(decision, Ask) or getattr(request.step, "side_effect", False)
        if needs_approval and not request.granted:
            approval = self._ctx.get("approval")
            if approval is None:
                return Deny("approval required, but no approval service is active")
            if approval.policy == "never":
                return Deny("approval required, and the approval policy is 'never'")
            if isinstance(decision, Ask):
                return decision
            return Ask(f"{request.step.name} has side effects outside RunSpool")
        for guard in self._steps.guards:
            reason = guard(request)
            if reason:
                return Deny(reason)
        return Allow()

    def asked(self, task: dict[str, Any]) -> None:
        approval = self._ctx.get("approval")
        if approval is not None:
            approval.requested(task)


class RuntimeService:
    def __init__(
        self, config: AppConfig, store: Store, steps: Any, startup: Any, ctx: Any = None
    ) -> None:
        self.config = config
        self.store = store
        self.steps = steps
        self._startup = startup
        self.gate = Gate(ctx, steps) if ctx is not None else None

    def ensure_ready(self) -> None:
        """Refuse to run the engine on a partially loaded plugin set.

        ``run`` and ``daemon`` start only when every enabled plugin is active and every
        lazily registered step imports cleanly. A failed plugin may have been meant to
        provide or override a step; running without it could silently run the wrong
        step. Disable plugins you do not want instead.
        """
        report = self._startup.report()
        problems = report.lines()
        if problems:
            raise StartupError(
                "cannot run: some plugins are not active (fix them, or disable them in the "
                "profile):\n  " + "\n  ".join(problems),
                report,
            )
        failures = self.steps.resolve_all()
        if failures:
            raise StartupError(
                "cannot run: some steps failed to load:\n  "
                + "\n  ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in failures),
                report,
            )

    def build_coordinator(self, *, notifier: Callable[[str], None] | None = None) -> Coordinator:
        registry = self.steps.registry
        runner_kwargs: dict[str, Any] = {"notifier": notifier} if notifier is not None else {}
        if self.gate is not None:
            runner_kwargs["gate"] = self.gate
            runner_kwargs["on_approval_asked"] = self.gate.asked
        store = self.store
        runner = TaskRunner(
            store.repo, store.log, store.step_runs, registry, self.config, **runner_kwargs
        )
        pool = WorkerPool(self.config.worker_pool.size)
        return Coordinator(store.repo, store.log, registry, runner, pool, self.config)

    def build_daemon(self) -> Daemon:
        self.ensure_ready()
        return Daemon(self.build_coordinator(), self.config)

    def recover(self) -> None:
        any_workflow = next(iter(self.config.workflows))
        StateMachine(
            self.store.repo,
            self.store.log,
            workflow=self.config.workflow(any_workflow),
            step_runs=self.store.step_runs,
        ).recover_interrupted()

    def run_until_idle(
        self,
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
        self.ensure_ready()
        coordinator = self.build_coordinator(notifier=notifier)
        self.recover()
        total_tasks = len(self.store.repo.list_all())
        max_steps = max((len(wf.steps) for wf in self.config.workflows.values()), default=1)
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

    # -- daemon process helpers ---------------------------------------------------

    def pid_file(self) -> Path:
        return Path(self.config.runtime_dir) / "daemon.pid"

    def daemon_status(self) -> dict[str, Any]:
        pid = read_pid(self.pid_file())
        if pid is not None and _pid_alive(pid):
            return {"running": True, "pid": pid}
        return {"running": False, "pid": None}

    def request_daemon_stop(self) -> bool:
        status = self.daemon_status()
        if not status["running"]:
            return False
        os.kill(status["pid"], signal.SIGTERM)
        return True


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


plugin = Plugin(
    name="runtime",
    apply=lambda ctx, config: ctx.provide(
        "runtime", RuntimeService(ctx.config, ctx.store, ctx.steps, ctx.startup, ctx)
    ),
    inject=["config", "store", "steps", "startup"],
)
