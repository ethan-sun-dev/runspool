"""Built-in steps: a small, dependency-free local-file pipeline.

These steps require no network, API keys, or external binaries. They exist to
make Runspool runnable the moment it is installed and to serve as readable
reference implementations of the step contract:

    ingest_file -> classify_text -> normalize_markdown -> summarize_text -> archive
"""

from __future__ import annotations

from runspool.builtin_steps.archive import ArchiveStep
from runspool.builtin_steps.file_intake import FileIntakeStep
from runspool.builtin_steps.markdown_normalize import MarkdownNormalizeStep
from runspool.builtin_steps.text_classify import TextClassifyStep
from runspool.builtin_steps.text_summarize import TextSummarizeStep
from runspool.engine.registry import StepRegistry

BUILTIN_STEP_CLASSES = (
    FileIntakeStep,
    TextClassifyStep,
    MarkdownNormalizeStep,
    TextSummarizeStep,
    ArchiveStep,
)

__all__ = [
    "FileIntakeStep",
    "TextClassifyStep",
    "MarkdownNormalizeStep",
    "TextSummarizeStep",
    "ArchiveStep",
    "BUILTIN_STEP_CLASSES",
    "register_builtins",
]


def register_builtins(registry: StepRegistry) -> None:
    """Register every built-in step into ``registry``."""
    for cls in BUILTIN_STEP_CLASSES:
        registry.register(cls())
