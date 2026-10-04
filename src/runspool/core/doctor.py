"""``doctor``: environment and plugin health checks.

Core checks cover Python, the workspace, the database, workflows, steps and the
plugins themselves. Plugins add their own with ``ctx.doctor.register(check)``,
where ``check()`` returns a :class:`Check` or a list of them.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from runspool.kernel import Plugin

CheckFn = Callable[[], "Check | list[Check]"]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


class DoctorService:
    def __init__(self, config: Any, store: Any, steps: Any, startup: Any) -> None:
        self._config = config
        self._store = store
        self._steps = steps
        self._startup = startup
        self._checks: list[tuple[str, CheckFn]] = []

    def for_context(self, ctx: Any) -> _BoundDoctor:
        return _BoundDoctor(self, ctx)

    def run(self) -> list[Check]:
        checks = self._core_checks()
        for owner, fn in list(self._checks):
            try:
                result = fn()
            except Exception as exc:  # noqa: BLE001 - a broken check is itself a finding
                checks.append(Check(owner, False, f"check raised {type(exc).__name__}: {exc}"))
                continue
            checks.extend(result if isinstance(result, list) else [result])
        return checks

    def _core_checks(self) -> list[Check]:
        config = self._config
        checks: list[Check] = []

        py_ok = sys.version_info >= (3, 11)
        checks.append(Check("python", py_ok, f"{sys.version.split()[0]} (>= 3.11 required)"))

        root = Path(config.workspace_root)
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".runspool-write-test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            writable, detail = True, str(root)
        except OSError as exc:
            writable, detail = False, f"{root}: {exc}"
        checks.append(Check("workspace_root", writable, detail))

        try:
            db_ok, db_detail = True, self._store.check()
        except Exception as exc:  # noqa: BLE001 - report any store failure as a check
            db_ok, db_detail = False, f"{config.database_path}: {exc}"
        checks.append(Check("database", db_ok, db_detail))

        n_workflows = len(config.workflows)
        detail = f"{n_workflows} defined"
        ok = n_workflows > 0
        if config.default_workflow is not None:
            if config.default_workflow in config.workflows:
                detail += f"; default: {config.default_workflow}"
            else:
                ok = False
                detail += f"; default_workflow {config.default_workflow!r} is not defined"
        checks.append(Check("workflows", ok, detail))

        # Every step referenced by a workflow resolves (built-in or plugin), and every
        # lazily registered step imports. This catches typos and broken plugin imports
        # before runtime.
        problems = self._startup.report().lines()
        failures = self._steps.resolve_all()
        missing: list[str] = []
        for wf in config.workflows.values():
            for step in wf.steps:
                if not self._steps.has(step) and step not in missing:
                    missing.append(step)
        if failures:
            detail = "; ".join(f"{name}: {type(exc).__name__}: {exc}" for name, exc in failures)
            checks.append(Check("steps", False, f"plugin load failed: {detail}"))
        elif missing and problems:
            checks.append(Check("steps", False, f"plugin load failed: {'; '.join(problems)}"))
        elif missing:
            checks.append(Check("steps", False, f"unregistered steps: {', '.join(missing)}"))
        else:
            checks.append(Check("steps", True, f"{len(self._steps.names())} registered"))

        checks.append(
            Check("plugins", not problems, "; ".join(problems) if problems else "all active")
        )
        warnings = list(getattr(self._startup, "warnings", []))
        checks.append(
            Check("profile", not warnings, "; ".join(warnings) if warnings else "no warnings")
        )
        return checks


class _BoundDoctor:
    def __init__(self, service: DoctorService, ctx: Any) -> None:
        self._service = service
        self._ctx = ctx

    def run(self) -> list[Check]:
        return self._service.run()

    def register(self, check: CheckFn) -> Callable[[], None]:
        entry = (self._ctx.fiber.name, check)
        checks = self._service._checks

        def register() -> Callable[[], None]:
            checks.append(entry)
            return lambda: checks.remove(entry) if entry in checks else None

        return self._ctx.effect(register, label="doctor-check")


plugin = Plugin(
    name="doctor",
    apply=lambda ctx, config: ctx.provide(
        "doctor", DoctorService(ctx.config, ctx.store, ctx.steps, ctx.startup)
    ),
    inject=["config", "store", "steps", "startup"],
)
