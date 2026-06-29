"""archive: move the task workspace into the ``ready/`` directory.

This bounds the local automation: once a task is archived, its artifacts live in
a stable, predictable location and the active workspace is freed.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from runspool.builtin_steps.workspace import archive_dir
from runspool.engine.step import Step, StepContext, StepResult


class ArchiveStep(Step):
    name = "archive"

    def run(self, ctx: StepContext) -> StepResult:
        # Resolve the workspace path without creating it: a re-execution after a
        # successful archive would otherwise recreate an empty dir and clobber
        # the real output.
        ws = Path(ctx.config.workspace_root) / "tasks" / str(ctx.task["id"])
        ready = archive_dir(ctx.config, ctx.task)

        # Idempotent on re-execution (e.g. after a reclaim): if the archive
        # already exists and the workspace is gone or empty, treat it as done.
        if ready.exists() and (not ws.exists() or not any(ws.iterdir())):
            return StepResult(message=f"already archived to {ready}")

        ready.parent.mkdir(parents=True, exist_ok=True)
        if not ready.exists():
            shutil.move(str(ws), str(ready))
            return StepResult(message=f"archived to {ready}")

        # Replace an existing archive crash-safely: rename it aside first, move
        # the new output into place, then drop the backup. If the move fails,
        # restore the previous archive so we never end up with neither.
        backup = ready.with_name(ready.name + ".bak")
        if backup.exists():
            shutil.rmtree(backup)
        os.replace(ready, backup)
        try:
            shutil.move(str(ws), str(ready))
        except Exception:
            os.replace(backup, ready)
            raise
        shutil.rmtree(backup)
        return StepResult(message=f"archived to {ready}")
