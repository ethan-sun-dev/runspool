import json

from runspool.builtin_steps.markdown_normalize import normalize
from runspool.builtin_steps.text_classify import classify
from runspool.builtin_steps.text_summarize import summarize
from runspool.commands import add_task
from runspool.models import TaskStatus
from runspool.runtime import run_until_idle


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


def test_missing_input_goes_manual_required(ctx):
    # max_retries default is 3; force it to 0 so one failure is terminal-ish fast.
    tid = add_task(ctx, "/no/such/file.txt", workflow="local_file")
    ctx.repo.update_fields(tid, {"max_retries": 0})
    run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "not found" in task["last_error"]
