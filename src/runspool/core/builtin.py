"""``builtin-*``: one plugin per built-in step, so a profile can disable or replace
any single one (e.g. ``{id: builtin-archive, disabled: true}``)."""

from __future__ import annotations

from runspool.builtin_steps import (
    ArchiveStep,
    FileIntakeStep,
    MarkdownNormalizeStep,
    TextClassifyStep,
    TextSummarizeStep,
)
from runspool.engine.step import Step
from runspool.kernel import Plugin


def step_plugin(cls: type[Step]) -> Plugin:
    """A plugin that registers one step class (instantiated with no arguments)."""
    return Plugin(
        name=f"builtin-{cls.name}",
        apply=lambda ctx, config: ctx.steps.register(cls()),
        inject=["steps"],
    )


ingest_file = step_plugin(FileIntakeStep)
classify_text = step_plugin(TextClassifyStep)
normalize_markdown = step_plugin(MarkdownNormalizeStep)
summarize_text = step_plugin(TextSummarizeStep)
archive = step_plugin(ArchiveStep)
