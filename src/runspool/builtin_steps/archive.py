"""archive: move the task workspace into the ``ready/`` directory.

This bounds the local automation: once a task is archived, its artifacts live in
a stable, predictable location and the active workspace is freed.
"""

from __future__ import annotations

import shutil

from runspool.builtin_steps.workspace import archive_dir, task_workspace
from runspool.engine.step import Step, StepContext, StepResult


class ArchiveStep(Step):
    name = "archive"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        ready = archive_dir(ctx.config, ctx.task)
        ready.parent.mkdir(parents=True, exist_ok=True)
        if ready.exists():
            shutil.rmtree(ready)
        shutil.move(str(ws), str(ready))
        return StepResult(message=f"archived to {ready}")
