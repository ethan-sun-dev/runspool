"""``runtime``: runner, scheduler (coordinator + worker pool) and daemon wiring."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
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
from runspool.executor_lock import ExecutorLock, executor_lock_path
from runspool.kernel import Plugin, StartupError
from runspool.persistence.state_machine import StateMachine

_contracts_log = logging.getLogger("runspool.contracts")


class Gate:
    """The pre-execute gate (see runspool.engine.gate), in this order:

    0. before any plugin runs, take what decides approval: whether the step has side
       effects (as recorded when it was registered) and whether this exact attempt
       was approved (from the task as stored). Listeners get a read-only task, and
       nothing they change afterwards affects these;
    1. plugins' ``step/pre-execute`` waterfall listeners decide (default: allow);
       anything that is not a decision, or an exception, refuses the step;
    2. ``steps.guard`` checks may refuse it (before anyone is asked to approve);
    3. a step with side effects, or an ``Ask``, needs approval unless this attempt
       was approved; with no active approval service, or policy ``never``, it is
       refused instead.

    Step 3 is RunSpool's own rule, applied after the plugins: no listener can wave a
    step with side effects through. (Plugins are trusted code: one that writes to
    the store directly, or approves tasks itself, is not stopped here; approvals by
    a plugin are recorded under that plugin's name.)
    """

    def __init__(self, ctx: Any, steps: Any, config: AppConfig | None = None) -> None:
        self._ctx = ctx
        self._steps = steps
        self._contracts = WorkflowContracts(steps, config) if config is not None else None

    def __call__(self, request: GateRequest) -> Decision:
        if self._contracts is not None:
            problem = self._contracts.problem(request.task["workflow"], request.step)
            if problem:
                return Deny(problem)
        try:
            side_effect = self._steps.side_effect_of(request.step.name)
        except Exception as exc:  # noqa: BLE001 - unknown side effects fail closed
            return Deny(f"cannot tell whether the step has side effects: {exc}")
        granted = request.granted
        try:
            decision = self._ctx.waterfall("step/pre-execute", request, default=Allow)
        except Exception as exc:  # noqa: BLE001 - a broken policy fails closed
            return Deny(f"pre-execute policy failed: {type(exc).__name__}: {exc}")
        if not isinstance(decision, DECISIONS):
            return Deny(f"pre-execute policy returned {decision!r}, not a decision")
        if isinstance(decision, (Deny, Defer)):
            return decision
        for guard in self._steps.guards:
            try:
                reason = guard(request)
            except Exception as exc:  # noqa: BLE001 - a broken guard fails closed
                return Deny(f"step guard failed: {type(exc).__name__}: {exc}")
            if reason:
                return Deny(reason)
        if (isinstance(decision, Ask) or side_effect) and not granted:
            approval = self._ctx.get("approval")
            if approval is None:
                return Deny("approval required, but no approval service is active")
            policy = getattr(approval, "policy", None)
            if policy != "ask":
                return Deny(f"approval required, and the approval policy is {policy!r}")
            if isinstance(decision, Ask):
                return decision
            return Ask(f"{request.step.name} has side effects outside RunSpool")
        return Allow()

    def asked(self, task: dict[str, Any]) -> None:
        approval = self._ctx.get("approval")
        if approval is not None:
            approval.requested(task)


class WorkflowContracts:
    """Artifact contracts of the workflow a task belongs to, checked before each of
    its steps runs (``run`` and the daemon alike, so tasks added after the daemon
    started are checked too). Doctor checks every workflow; this only the ones
    that have work.

    The registry is the one the runtime started with: plugin changes
    (``fiber.restart()`` and the like) are refused until the runtime restarts
    (see ``runspool.core.boot``). The step about to run is
    checked as the object that will run, not as whatever is registered under its
    name by then.
    """

    def __init__(self, steps: Any, config: AppConfig) -> None:
        self._steps = steps
        self._config = config
        # Unverified gaps already warned about, per workflow: warn once, not on
        # every step.
        self._warned: dict[str, tuple[str, ...]] = {}
        self._lock = threading.Lock()

    def problem(self, workflow: str, step: Any = None) -> str | None:
        """Why ``step`` of a task of ``workflow`` may not run now, or ``None``."""
        definition = self._config.workflows.get(workflow)
        if definition is None:
            return None  # an unknown workflow is the scheduler's business
        report = self._steps.check_contract(workflow, definition, running=step)
        if report.errors:
            return f"artifact contracts of workflow {workflow!r} are not met: " + "; ".join(
                report.errors
            )
        unverified = tuple(report.unverified)
        with self._lock:
            changed = unverified and self._warned.get(workflow) != unverified
            self._warned[workflow] = unverified
        if changed:
            _contracts_log.warning(
                "workflow %r runs with unverified artifact contracts: %s",
                workflow,
                "; ".join(unverified),
            )
        return None


class RuntimeService:
    def __init__(
        self, config: AppConfig, store: Store, steps: Any, startup: Any, ctx: Any = None
    ) -> None:
        self.config = config
        self.store = store
        self.steps = steps
        self._startup = startup
        self._ctx = ctx
        self.gate = Gate(ctx, steps, config) if ctx is not None else None

    def ensure_ready(self) -> None:
        """Refuse to run the engine on a partially loaded plugin set.

        ``run`` and ``daemon`` start only when every enabled plugin is active and every
        lazily registered step imports cleanly. A failed plugin may have been meant to
        provide or override a step; running without it could silently run the wrong
        step. Disable plugins you do not want instead.

        Once ready, the plugin set is fixed: later plugin changes are refused. So the
        runtime cannot start while plugins are still being loaded or unloaded (from
        inside a plugin's ``apply`` or a disposer): what that plugin registers next
        would not be fixed yet. Nothing is started or fixed then.
        """
        report = self._startup.report()
        changing = getattr(self._startup, "plugins_changing", None)
        if callable(changing) and changing():
            raise StartupError(
                "cannot run: plugins are still being loaded or unloaded; start the runtime "
                "after loading has finished (not from inside a plugin's apply or disposer)",
                report,
            )
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
        # From here on the plugin set is fixed: every plugin change is refused until
        # the runtime restarts, so steps (and their artifact contracts) are the ones
        # checked now (see runspool.core.boot).
        fix = getattr(self._startup, "fix_plugin_set", None)
        if callable(fix):
            fix()

    def build_coordinator(self, *, notifier: Callable[[str], None] | None = None) -> Coordinator:
        """A coordinator (task runner + worker pool) that executes steps. Like every
        way to start executing, it first passes :meth:`ensure_ready` (which refuses
        while plugins are loading, and fixes the plugin set), so nothing executes
        steps from a plugin set that is still changing."""
        self.ensure_ready()
        return self._build_coordinator(notifier=notifier)

    def _build_coordinator(self, *, notifier: Callable[[str], None] | None = None) -> Coordinator:
        """Build without checking; callers have passed :meth:`ensure_ready`."""
        registry = self.steps.registry
        runner_kwargs: dict[str, Any] = {"notifier": notifier} if notifier is not None else {}
        if self.gate is not None:
            runner_kwargs["gate"] = self.gate
            runner_kwargs["on_approval_asked"] = self.gate.asked
        store = self.store
        runner = TaskRunner(
            store.repo,
            store.log,
            store.step_runs,
            registry,
            self.config,
            artifacts=getattr(store, "artifacts", None),
            items=getattr(store, "items", None),
            **runner_kwargs,
        )
        pool = WorkerPool(self.config.worker_pool.size)
        return Coordinator(store.repo, store.log, registry, runner, pool, self.config)

    def build_daemon(self) -> Daemon:
        """The resident daemon. After each round it emits ``daemon/tick`` with a
        :class:`~runspool.daemon.DaemonTick`, so plugins can do periodic maintenance
        (sweep caches, rotate files). Listeners run on the daemon thread: keep them
        quick, throttle them yourself (``tick.now``), and hand long work to a thread.
        A listener that raises is logged and skipped. ``run`` / ``run_until_idle``
        do not emit it: it belongs to the long-running process."""
        self.ensure_ready()
        ctx = self._ctx
        on_tick = (lambda tick: ctx.emit("daemon/tick", tick)) if ctx is not None else None
        return Daemon(
            self._build_coordinator(), self.config, on_tick=on_tick, lock=self.executor_lock()
        )

    def executor_lock(self) -> ExecutorLock:
        """The lock that makes this process the only one executing steps on the
        database (see :mod:`runspool.executor_lock`)."""
        return ExecutorLock(executor_lock_path(self.config.database_path))

    def recover(self) -> None:
        any_workflow = next(iter(self.config.workflows))
        StateMachine(
            self.store.repo,
            self.store.log,
            workflow=self.config.workflow(any_workflow),
            step_runs=self.store.step_runs,
            items=getattr(self.store, "items", None),
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

        It runs only while holding the executor lock: if another process (a daemon,
        another ``run``) is executing tasks on this database, it raises
        :class:`~runspool.executor_lock.ExecutorBusy` without touching anything.
        Crash recovery happens under the lock too, so it never requeues work a live
        process is running.
        """
        self.ensure_ready()
        lock = self.executor_lock()
        if not lock.acquire():
            raise lock.busy_error("cannot run")
        try:
            coordinator = self._build_coordinator(notifier=notifier)
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
        finally:
            lock.release()

    # -- daemon process helpers ---------------------------------------------------

    def pid_file(self) -> Path:
        return Path(self.config.runtime_dir) / "daemon.pid"

    def daemon_status(self) -> dict[str, Any]:
        """``running``: the daemon process exists. ``stopped``: it exists but is
        suspended (SIGSTOP / SIGTTIN, e.g. a background job that touched the
        terminal): it holds its tasks and schedules nothing until resumed."""
        pid = read_pid(self.pid_file())
        if pid is not None and _pid_alive(pid):
            return {"running": True, "pid": pid, "stopped": _pid_state(pid) in ("T", "t")}
        return {"running": False, "pid": None, "stopped": False}

    def request_daemon_stop(self) -> bool:
        status = self.daemon_status()
        if not status["running"]:
            return False
        os.kill(status["pid"], signal.SIGTERM)
        return True


def _pid_state(pid: int) -> str | None:
    """The process state letter (``R``, ``S``, ``T`` ...), or ``None`` if it cannot tell."""
    try:  # Linux
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        pass
    try:  # macOS / BSD
        out = subprocess.run(
            ["ps", "-o", "stat=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=5,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    state = out.stdout.strip()
    return state[0] if state else None


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
