import json
from pathlib import Path

import pytest

from runspool.builtin_steps import archive as archive_mod
from runspool.builtin_steps.archive import ArchiveStep
from runspool.builtin_steps.markdown_normalize import normalize
from runspool.builtin_steps.text_classify import classify
from runspool.builtin_steps.text_summarize import summarize
from runspool.commands import add_task
from runspool.engine.step import StepContext
from runspool.models import TaskStatus
from runspool.runtime import run_until_idle


def _archive_ctx(config, task_id=1):
    return StepContext(
        task={"id": task_id},
        config=config,
        should_stop=lambda: False,
        heartbeat=lambda *a, **k: None,
    )


def _ws_dir(config, task_id=1):
    return Path(config.workspace_root) / "tasks" / str(task_id)


def _ready_dir(config, task_id=1):
    return Path(config.workspace_root) / "ready" / str(task_id)


def test_classify_invoice():
    text = "Invoice 100\nBill to Acme\nSubtotal 5\nTotal amount due 5\nPayment terms net 30"
    result = classify(text)
    assert result["category"] == "invoice"
    assert result["confidence"] > 0


def test_classify_general_when_no_match():
    assert classify("the quick brown fox")["category"] == "general"


def test_normalize_adds_heading_and_collapses_blanks():
    out = normalize("body line\n\n\n\nmore", title="My Doc")
    assert out.startswith("# My Doc")
    assert "\n\n\n" not in out
    assert out.endswith("\n")


def test_normalize_keeps_existing_heading():
    out = normalize("# Already\n\ntext", title="Ignored")
    assert out.startswith("# Already")


def test_summarize_counts_and_keywords():
    s = summarize("Runspool runs workflows. Runspool is local. Workflows are resumable.")
    assert s["word_count"] > 0
    assert s["sentence_count"] == 3
    assert "runspool" in s["keywords"] or "workflows" in s["keywords"]


def test_full_pipeline_end_to_end(ctx, tmp_path):
    src = tmp_path / "ticket.txt"
    src.write_text(
        "Support ticket: the app shows an error and cannot start. Please reproduce.",
        encoding="utf-8",
    )
    tid = add_task(ctx, str(src), workflow="local_file")
    run_until_idle(ctx, notifier=lambda m: None)

    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.COMPLETED
    assert task["name"] == "ticket"

    ready = ctx.config.workspace_root / "ready" / str(tid)
    assert (ready / "source.txt").exists()
    assert (ready / "normalized.md").exists()
    assert (ready / "summary.md").exists()
    classification = json.loads((ready / "classification.json").read_text())
    assert classification["category"] == "support_ticket"

    # Every step recorded an ok run.
    statuses = {r["step"]: r["status"] for r in ctx.step_runs.list_for_task(tid)}
    assert statuses == {
        "ingest_file": "ok",
        "classify_text": "ok",
        "normalize_markdown": "ok",
        "summarize_text": "ok",
        "archive": "ok",
    }


def test_archive_moves_workspace_to_ready(ctx):
    ws = _ws_dir(ctx.config)
    ws.mkdir(parents=True)
    (ws / "out.txt").write_text("v1", encoding="utf-8")

    ArchiveStep().run(_archive_ctx(ctx.config))

    ready = _ready_dir(ctx.config)
    assert (ready / "out.txt").read_text(encoding="utf-8") == "v1"
    assert not ws.exists()


def test_archive_is_idempotent_when_already_archived(ctx):
    # Re-execution (e.g. after a reclaim) must not clobber a completed archive
    # with an empty/recreated workspace.
    ready = _ready_dir(ctx.config)
    ready.mkdir(parents=True)
    (ready / "out.txt").write_text("real", encoding="utf-8")
    ws = _ws_dir(ctx.config)
    ws.mkdir(parents=True)  # empty workspace, as if recreated

    ArchiveStep().run(_archive_ctx(ctx.config))

    assert (ready / "out.txt").read_text(encoding="utf-8") == "real"


def test_archive_preserves_previous_archive_on_failure(ctx, monkeypatch):
    # If the move fails midway, the previously archived output must survive
    # rather than being destroyed before the move.
    ready = _ready_dir(ctx.config)
    ready.mkdir(parents=True)
    (ready / "out.txt").write_text("v1", encoding="utf-8")
    ws = _ws_dir(ctx.config)
    ws.mkdir(parents=True)
    (ws / "out.txt").write_text("v2", encoding="utf-8")

    def boom(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(archive_mod.shutil, "move", boom)
    with pytest.raises(RuntimeError):
        ArchiveStep().run(_archive_ctx(ctx.config))

    assert (ready / "out.txt").read_text(encoding="utf-8") == "v1"


def test_missing_input_auto_retries_to_manual_required(ctx):
    # Default budget is 3 retries with retry_delay 0; run_until_idle should
    # automatically consume all retries and land in manual_required.
    tid = add_task(ctx, "/no/such/file.txt", workflow="local_file")
    run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert task["retry_count"] == 4  # initial attempt + 3 retries
    assert "not found" in task["last_error"]
