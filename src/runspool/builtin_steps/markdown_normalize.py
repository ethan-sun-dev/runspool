"""normalize_markdown: tidy the source text into clean Markdown."""

from __future__ import annotations

import re

from runspool.builtin_steps.workspace import read_source, task_workspace
from runspool.engine.step import Step, StepContext, StepResult

_BLANK_RUN = re.compile(r"\n{3,}")


def normalize(text: str, *, title: str | None = None) -> str:
    # Strip trailing whitespace from every line.
    lines = [line.rstrip() for line in text.splitlines()]
    body = "\n".join(lines).strip("\n")
    # Collapse runs of 3+ blank lines down to a single blank line.
    body = _BLANK_RUN.sub("\n\n", body)
    # Ensure the document opens with a level-1 heading.
    has_heading = body.lstrip().startswith("#")
    if not has_heading and title:
        body = f"# {title}\n\n{body}"
    return body.rstrip("\n") + "\n"


class MarkdownNormalizeStep(Step):
    name = "normalize_markdown"

    def run(self, ctx: StepContext) -> StepResult:
        ws = task_workspace(ctx.config, ctx.task)
        text = read_source(ws)
        title = ctx.task.get("name") or "Document"
        normalized = normalize(text, title=title)
        (ws / "normalized.md").write_text(normalized, encoding="utf-8")
        return StepResult(message=f"normalized to {len(normalized.splitlines())} lines")
