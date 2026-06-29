"""doctor: check the local environment Runspool needs to run."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from runspool.app import AppContext
from runspool.registry_builder import StepLoadError, build_registry


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_doctor(ctx: AppContext) -> list[Check]:
    checks: list[Check] = []

    # Python version.
    py_ok = sys.version_info >= (3, 11)
    checks.append(
        Check("python", py_ok, f"{sys.version.split()[0]} (>= 3.11 required)")
    )

    # Workspace writable.
    root = Path(ctx.config.workspace_root)
    try:
        root.mkdir(parents=True, exist_ok=True)
        probe = root / ".runspool-write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        writable = True
        detail = str(root)
    except OSError as exc:
        writable = False
        detail = f"{root}: {exc}"
    checks.append(Check("workspace_root", writable, detail))

    # Database reachable / initialised.
    try:
        ctx.db.init()
        with ctx.db.connect() as conn:
            conn.execute("select 1").fetchone()
        db_ok = True
        db_detail = str(ctx.config.database_path)
    except Exception as exc:  # noqa: BLE001 - report any DB failure as a check
        db_ok = False
        db_detail = f"{ctx.config.database_path}: {exc}"
    checks.append(Check("database", db_ok, db_detail))

    # At least one workflow defined.
    n_workflows = len(ctx.config.workflows)
    checks.append(Check("workflows", n_workflows > 0, f"{n_workflows} defined"))

    # Every step referenced by a workflow resolves in the registry (built-in or
    # plugin). This catches typos and broken plugin imports before runtime.
    try:
        registry = build_registry(ctx.config)
        missing: list[str] = []
        for wf in ctx.config.workflows.values():
            for step in wf.steps:
                if not registry.has(step) and step not in missing:
                    missing.append(step)
        if missing:
            checks.append(
                Check("steps", False, f"unregistered steps: {', '.join(missing)}")
            )
        else:
            checks.append(
                Check("steps", True, f"{len(registry.names())} registered")
            )
    except StepLoadError as exc:
        checks.append(Check("steps", False, f"plugin load failed: {exc}"))

    return checks
