"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from runspool.app import AppContext, load_context


def write_config(
    tmp_path: Path,
    *,
    steps: tuple[str, ...] = ("ingest_file", "classify_text", "normalize_markdown",
                              "summarize_text", "archive"),
    extra: str = "",
) -> Path:
    """Write a minimal config.yaml and return its path."""
    steps_yaml = ", ".join(steps)
    text = (
        f"workspace_root: {tmp_path / 'workspace'}\n"
        f"workflows:\n  local_file:\n    steps: [{steps_yaml}]\n"
        f"{extra}"
    )
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture
def config_path(tmp_path: Path) -> Path:
    return write_config(tmp_path)


@pytest.fixture
def ctx(config_path: Path) -> AppContext:
    return load_context(config_path)
