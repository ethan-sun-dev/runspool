"""ingest_file: read the task input file into the workspace and record metadata."""

from __future__ import annotations

import json
from pathlib import Path

from runspool.builtin_steps.workspace import task_workspace
from runspool.engine.step import Step, StepContext, StepResult


class FileIntakeStep(Step):
    name = "ingest_file"

    def run(self, ctx: StepContext) -> StepResult:
        src = Path(ctx.task["input"]).expanduser()
        if not src.exists():
            raise FileNotFoundError(f"input file not found: {src}")
        if not src.is_file():
            raise ValueError(f"input is not a file: {src}")

        ctx.heartbeat("reading input")
        text = src.read_text(encoding="utf-8", errors="replace")
        ws = task_workspace(ctx.config, ctx.task)
        (ws / "source.txt").write_text(text, encoding="utf-8")

        metadata = {
            "original_path": str(src),
            "original_name": src.name,
            "suffix": src.suffix,
            "size_bytes": src.stat().st_size,
            "line_count": text.count("\n") + (1 if text and not text.endswith("\n") else 0),
            "word_count": len(text.split()),
            "char_count": len(text),
        }
        (ws / "metadata.json").write_text(
            json.dumps(metadata, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )

        # Give the task a readable name if it does not have one yet.
        updates = {}
        if not ctx.task.get("name"):
            updates["name"] = src.stem
        return StepResult(
            message=f"ingested {src.name} ({metadata['word_count']} words)", updates=updates
        )
