"""``add_task`` keeps an input that cannot be a path as it is (issue #26).

The input is checked as a possible file so an existing file is stored with an
absolute path. Plain text longer than the platform's path limit made that check
raise ``OSError`` (File name too long), and an embedded NUL makes it raise
``ValueError``; either way the task was never created.
"""

from __future__ import annotations

import pytest

from runspool import commands
from runspool.app import load_context
from tests.conftest import write_config


@pytest.fixture
def ctx(tmp_path):
    return load_context(write_config(tmp_path))


@pytest.mark.parametrize(
    "text",
    [
        "x" * 5000,  # far longer than any path limit
        "字" * 2000,  # multi-byte: over the limit in bytes
        "a line\nanother line " * 300,
        "embedded\x00nul",
    ],
)
def test_text_that_cannot_be_a_path_is_kept_as_input(ctx, text):
    tid = commands.add_task(ctx, text, workflow="local_file")
    assert ctx.repo.get_task(tid)["input"] == text


def test_an_existing_file_is_still_stored_with_an_absolute_path(ctx, tmp_path, monkeypatch):
    doc = tmp_path / "note.txt"
    doc.write_text("hello", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    tid = commands.add_task(ctx, "note.txt", workflow="local_file")
    assert ctx.repo.get_task(tid)["input"] == str(doc.resolve())


def test_short_text_is_unchanged(ctx):
    tid = commands.add_task(ctx, "just a short sentence", workflow="local_file")
    assert ctx.repo.get_task(tid)["input"] == "just a short sentence"


def test_a_file_in_an_unreadable_directory_still_raises(ctx, tmp_path):
    # Only "this cannot be a path" is swallowed; a permission problem on a real
    # path is reported as before rather than stored as plain text.
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "note.txt").write_text("hello", encoding="utf-8")
    locked.chmod(0o000)
    try:
        with pytest.raises(PermissionError):
            commands.add_task(ctx, str(locked / "note.txt"), workflow="local_file")
    finally:
        locked.chmod(0o700)
