"""External steps: work items opened, claimed, submitted, validated by the executor."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import textwrap
import threading

import pytest
from typer.testing import CliRunner

from runspool import commands
from runspool.app import load_context
from runspool.cli import app
from runspool.engine.external import ExternalStep, Submission, SubmissionRejected
from runspool.engine.step import StepResult
from runspool.models import TaskStatus
from runspool.persistence.connection import Database
from runspool.persistence.state_machine import IllegalTransition
from runspool.persistence.work_items import WorkItems
from runspool.runtime import run_until_idle

STEPS = """
from pathlib import Path

from runspool.engine.artifacts import Artifact
from runspool.engine.external import ExternalStep, SubmissionRejected, WorkRequest
from runspool.engine.step import Step, StepResult

STATE = {"crash": False, "run_last": True, "hold": None}


class Draft(Step):
    name = "draft"

    def run(self, ctx):
        return StepResult(message="drafted")


class Review(ExternalStep):
    name = "review"
    max_attempts = 2
    provides = ("review",)

    def request(self, ctx):
        return WorkRequest(
            kind="human",
            instructions="校对「笔记」",
            payload={"input": ctx.task["input"], "attempt": ctx.attempt},
        )

    def validate(self, ctx, submission):
        if STATE["hold"] is not None:  # (entered, go): hold the step's slot until "go"
            entered, go = STATE["hold"]
            entered.set()
            go.wait(10)
        if STATE["crash"]:
            raise RuntimeError("validator broke")
        path = submission.file(ctx.config.workspace_root)
        text = path.read_text(encoding="utf-8")
        if "bad" in text:
            raise SubmissionRejected(f"attempt {submission.attempt}: contains bad")
        return StepResult(
            message="reviewed",
            updates={"metadata": {"reviewed_by": submission.submitted_by}},
            artifacts=[Artifact("review", str(path))],
        )


class LastReview(Review):
    name = "last_review"

    def when(self, task, config):
        return STATE["run_last"]


class Degrade(Step):
    name = "degrade"

    def run(self, ctx):
        return StepResult(degraded=True, message="only partly")


class Finish(Step):
    name = "finish"
    requires = ("review",)

    def run(self, ctx):
        return StepResult(message="finished")
"""


@pytest.fixture
def ws(tmp_path, monkeypatch, request):
    module = f"external_steps_{request.node.name.replace('[', '_').replace(']', '_')}"
    (tmp_path / f"{module}.py").write_text(STEPS, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, module, raising=False)
    path = tmp_path / "配置 目录" / "runspool.yaml"
    path.parent.mkdir()
    steps = "".join(
        f'  {n}:\n    import: "{module}:{c}"\n'
        for n, c in (
            ("draft", "Draft"),
            ("review", "Review"),
            ("finish", "Finish"),
            ("last_review", "LastReview"),
            ("degrade", "Degrade"),
        )
    )
    path.write_text(
        f"workspace_root: {tmp_path / '工作 区'}\n"
        "scheduler:\n  max_retries: 0\n"
        f"steps:\n{steps}"
        "workflows:\n  drafts:\n    steps: [draft, review, finish]\n"
        "  tail:\n    steps: [draft, last_review]\n"
        "  tail_degraded:\n    steps: [degrade, last_review]\n",
        encoding="utf-8",
    )
    ctx = load_context(path)
    return ctx, path, module


def _new_task(ctx):
    return commands.add_task(ctx, "稿件 一", workflow="drafts")


def _file(tmp_path, text, name="结果.md"):
    out = tmp_path / "提交 目录" / name
    out.parent.mkdir(exist_ok=True)
    out.write_text(text, encoding="utf-8")
    return out, hashlib.sha256(out.read_bytes()).hexdigest()


def _submit(ctx, item_id, path, sha, *, by="agent"):
    token = commands.claim_item(ctx, item_id, by=by)
    commands.submit_item(ctx, item_id, token=token, value={"path": str(path), "sha256": sha}, by=by)


def _status(ctx, tid):
    return ctx.repo.get_task(tid)["task_status"]


def test_a_task_waits_for_external_work_and_advances_once_it_is_accepted(ws, tmp_path):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(tid)
    assert (task["task_status"], task["step"]) == (TaskStatus.AWAITING_EXTERNAL, "review")
    (item,) = ctx.items.list_items(task_id=tid)
    assert item["status"] == "open" and item["kind"] == "human"
    assert item["instructions"] == "校对「笔记」"
    assert item["payload"] == {"input": "稿件 一", "attempt": 1}
    assert (item["attempt"], item["max_attempts"], item["previous_error"]) == (1, 2, None)

    path, sha = _file(tmp_path, "通过的稿件")
    _submit(ctx, item["id"], path, sha, by="agent:一号")
    assert _status(ctx, tid) == TaskStatus.QUEUED
    run_until_idle(ctx, notifier=lambda m: None)

    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.COMPLETED
    assert task["metadata"] == {"reviewed_by": "agent:一号"}
    item = ctx.items.get(item["id"])
    assert item["status"] == "accepted" and item["submission"]["path"] == str(path)
    runs = {r["step"]: r for r in ctx.step_runs.list_for_task(tid)}
    assert runs["review"]["status"] == "ok" and runs["review"]["note"] == "reviewed"
    assert item["step_run_id"] == runs["review"]["id"]
    (artifact,) = ctx.service("store").artifacts.latest(tid)
    assert artifact["path"] == str(path) and artifact["step_run_id"] == item["step_run_id"]
    events = [e["event_type"] for e in reversed(ctx.log.list_for_task(tid, limit=50))]
    for kind in ("item_opened", "item_claimed", "item_submitted", "completed"):
        assert kind in events
    checks = {c.name: c for c in ctx.service("doctor").run()}
    assert checks["artifacts"].ok  # an external step takes part in artifact contracts


def test_the_daemon_validates_a_submission_on_its_next_round(ws, tmp_path):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    daemon = ctx.service("runtime").build_daemon()
    try:

        def rounds(n):
            for _ in range(n):
                daemon.run_once()
                daemon.coordinator.pool.drain()

        rounds(3)
        assert _status(ctx, tid) == TaskStatus.AWAITING_EXTERNAL
        rounds(2)  # nothing to do while it waits
        assert len(ctx.items.list_items(task_id=tid)) == 1
        _submit(ctx, ctx.items.active_for(tid)["id"], *_file(tmp_path, "good"))
        rounds(3)
        assert _status(ctx, tid) == TaskStatus.COMPLETED
    finally:
        daemon.coordinator.pool.shutdown(wait=True)


def test_a_validation_interrupted_by_a_crash_is_done_again(ws, tmp_path):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(tid)
    _submit(ctx, item["id"], *_file(tmp_path, "good"))
    sm = ctx.state_machine("drafts")
    assert sm.claim(tid, worker="crashed")  # a worker took it, then the process died
    ctx.step_runs.start(tid, "review")
    sm.recover_interrupted()
    assert _status(ctx, tid) == TaskStatus.QUEUED
    assert ctx.items.get(item["id"])["status"] == "submitted"
    run_until_idle(ctx, notifier=lambda m: None)
    assert _status(ctx, tid) == TaskStatus.COMPLETED
    assert ctx.items.get(item["id"])["status"] == "accepted"


def test_an_external_step_records_only_artifacts_it_provides(ws, tmp_path, monkeypatch):
    from runspool.engine.artifacts import Artifact

    ctx, _, module = ws
    review = __import__(module).Review  # steps from config are imported lazily
    monkeypatch.setattr(
        review,
        "validate",
        lambda self, c, s: StepResult(artifacts=[Artifact("other", "x.md", "ab" * 32)]),
    )
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(tid)
    _submit(ctx, item["id"], *_file(tmp_path, "good"))
    run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "'other'" in task["last_error"]
    assert ctx.service("store").artifacts.latest(tid) == []
    assert ctx.items.get(item["id"])["status"] == "submitted"


def test_validation_runs_in_the_executor_not_in_the_submitter(ws, tmp_path, monkeypatch):
    ctx, _, module = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    (item,) = ctx.items.list_items(task_id=tid)
    calls = []
    review = sys.modules[module].Review
    original = review.validate
    monkeypatch.setattr(
        review, "validate", lambda self, c, s: calls.append(1) or original(self, c, s)
    )
    _submit(ctx, item["id"], *_file(tmp_path, "ok"))
    assert calls == []  # submitting only records the submission
    assert ctx.items.get(item["id"])["status"] == "submitted"
    assert [r["step"] for r in ctx.step_runs.list_for_task(tid)] == ["draft"]
    run_until_idle(ctx, notifier=lambda m: None)
    assert calls == [1] and _status(ctx, tid) == TaskStatus.COMPLETED


def test_a_rejected_submission_opens_the_next_attempt_with_the_reason(ws, tmp_path):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    (first,) = ctx.items.list_items(task_id=tid)
    _submit(ctx, first["id"], *_file(tmp_path, "bad draft"))
    run_until_idle(ctx, notifier=lambda m: None)

    first = ctx.items.get(first["id"])
    assert first["status"] == "rejected" and first["error"] == "attempt 1: contains bad"
    second = ctx.items.active_for(tid)
    assert second["id"] != first["id"] and second["status"] == "open"
    assert (second["attempt"], second["previous_error"]) == (2, "attempt 1: contains bad")
    assert second["payload"]["attempt"] == 2  # the request saw the attempt
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.AWAITING_EXTERNAL
    assert task["last_error"] == "attempt 1: contains bad"
    assert task["retry_count"] == 0  # a rejection is not a step failure
    review_runs = [r for r in ctx.step_runs.list_for_task(tid) if r["step"] == "review"]
    assert [r["status"] for r in review_runs] == ["rejected"]


def test_rejections_past_the_limit_need_a_human_and_retry_starts_over(ws, tmp_path):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    for attempt in (1, 2):
        item = ctx.items.active_for(tid)
        assert item["attempt"] == attempt
        _submit(ctx, item["id"], *_file(tmp_path, "bad", name=f"{attempt}.md"))
        run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "rejected 2 time(s), no attempts left" in task["last_error"]
    assert ctx.items.active_for(tid) is None
    assert [i["status"] for i in ctx.items.list_items(task_id=tid)] == ["rejected", "exhausted"]

    commands.retry_task(ctx, tid)
    run_until_idle(ctx, notifier=lambda m: None)
    fresh = ctx.items.active_for(tid)
    assert (fresh["attempt"], fresh["previous_error"]) == (1, None)
    _submit(ctx, fresh["id"], *_file(tmp_path, "good", name="3.md"))
    run_until_idle(ctx, notifier=lambda m: None)
    assert _status(ctx, tid) == TaskStatus.COMPLETED


def test_a_validator_that_raises_is_a_step_failure_and_keeps_the_submission(ws, tmp_path):
    ctx, _, module = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(tid)
    _submit(ctx, item["id"], *_file(tmp_path, "good"))
    sys.modules[module].STATE["crash"] = True
    run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED  # max_retries is 0
    assert "validator broke" in task["last_error"]
    assert ctx.items.get(item["id"])["status"] == "submitted"  # kept, checked again

    sys.modules[module].STATE["crash"] = False
    commands.retry_task(ctx, tid)
    run_until_idle(ctx, notifier=lambda m: None)
    assert _status(ctx, tid) == TaskStatus.COMPLETED
    assert ctx.items.get(item["id"])["status"] == "accepted"


def test_a_file_that_does_not_match_its_sha256_is_rejected(ws, tmp_path):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(tid)
    path, _ = _file(tmp_path, "good")
    _submit(ctx, item["id"], path, "0" * 64)
    run_until_idle(ctx, notifier=lambda m: None)
    rejected = ctx.items.get(item["id"])
    assert rejected["error"] == "the submitted file does not match its sha256"
    assert str(path) not in (ctx.repo.get_task(tid)["last_error"] or "")


def test_submitting_needs_a_claim_and_its_token(ws, tmp_path):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(tid)
    value = {"value": "x"}
    with pytest.raises(commands.ItemUnavailable, match="it is open"):
        commands.submit_item(ctx, item["id"], token="nope", value=value)
    token = commands.claim_item(ctx, item["id"], by="a")
    with pytest.raises(commands.ItemUnavailable, match="it is claimed"):
        commands.claim_item(ctx, item["id"], by="b")
    with pytest.raises(commands.ItemUnavailable, match="not claimed with that token"):
        commands.submit_item(ctx, item["id"], token="wrong", value=value)
    assert _status(ctx, tid) == TaskStatus.AWAITING_EXTERNAL  # nothing applied
    assert ctx.items.get(item["id"])["status"] == "claimed"
    with pytest.raises(ValueError, match="JSON object"):
        commands.submit_item(ctx, item["id"], token=token, value=["x"])  # type: ignore[arg-type]

    commands.release_item(ctx, item["id"], token=token)
    assert ctx.items.get(item["id"])["status"] == "open"
    token = commands.claim_item(ctx, item["id"], by="b")
    commands.submit_item(ctx, item["id"], token=token, value=value, by="b")
    with pytest.raises(commands.ItemUnavailable, match="it is submitted"):
        commands.submit_item(ctx, item["id"], token=token, value=value)
    with pytest.raises(KeyError):
        commands.claim_item(ctx, 999, by="a")


def test_only_one_of_many_concurrent_claimants_wins(ws):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    item_id = ctx.items.active_for(tid)["id"]
    claimants = 12
    barrier = threading.Barrier(claimants)
    tokens: list[str | None] = []
    lock = threading.Lock()

    def claim(n):
        items = WorkItems(Database(ctx.db.path))  # its own connections, like another client
        barrier.wait()
        token = items.claim(item_id, by=f"client-{n}")
        with lock:
            tokens.append(token)

    threads = [threading.Thread(target=claim, args=(n,)) for n in range(claimants)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    won = [t for t in tokens if t is not None]
    assert len(tokens) == claimants and len(won) == 1
    item = ctx.items.get(item_id)
    assert item["claim_token"] == won[0]
    claimed = [
        e for e in ctx.log.list_for_task(tid, limit=100) if e["event_type"] == "item_claimed"
    ]
    assert len(claimed) == 1


def test_one_task_never_has_two_active_items(ws):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    hook = ctx.items.opener(
        tid,
        "review",
        attempt=9,
        max_attempts=9,
        kind="x",
        instructions="",
        payload={},
        previous_error=None,
    )
    with sqlite3.connect(ctx.db.path) as conn, pytest.raises(sqlite3.IntegrityError):
        hook(conn)


def test_terminating_a_waiting_task_cancels_its_item(ws):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(tid)
    commands.terminate_task(ctx, tid)
    assert _status(ctx, tid) == TaskStatus.TERMINATED
    assert ctx.items.get(item["id"])["status"] == "cancelled"
    with pytest.raises(commands.ItemUnavailable):
        commands.claim_item(ctx, item["id"], by="a")


def test_moving_a_task_to_another_step_cancels_its_item(ws, tmp_path, monkeypatch):
    ctx, _, module = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(tid)
    _submit(ctx, item["id"], *_file(tmp_path, "good"))
    sys.modules[module].STATE["crash"] = True
    run_until_idle(ctx, notifier=lambda m: None)  # manual_required, item still submitted
    commands.set_step(ctx, tid, "finish")
    assert ctx.items.get(item["id"])["status"] == "cancelled"


def test_a_waiting_task_offers_terminate_and_items_not_pause(ws):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    with pytest.raises(IllegalTransition):
        commands.pause_task(ctx, tid)
    with pytest.raises(IllegalTransition):
        commands.set_step(ctx, tid, "finish", force=True)
    from runspool.views import inspect_view

    view = inspect_view(ctx, ctx.repo.get_task(tid))
    item = ctx.items.active_for(tid)
    assert view["available_actions"] == ["items", "terminate"]
    assert f"runspool items show {item['id']}" in view["suggested_next_action"]


def test_a_pause_requested_before_the_item_opens_pauses_in_place(ws):
    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None, max_rounds=1)  # draft done, review queued
    assert ctx.repo.get_task(tid)["step"] == "review"
    commands.pause_task(ctx, tid)
    assert _status(ctx, tid) == TaskStatus.PAUSED and ctx.items.active_for(tid) is None
    commands.resume_task(ctx, tid)
    run_until_idle(ctx, notifier=lambda m: None)
    assert ctx.items.active_for(tid)["attempt"] == 1


def test_a_store_without_work_items_fails_an_external_step(tmp_path):
    from runspool.engine.registry import StepRegistry
    from runspool.engine.runner import TaskRunner

    class Ask(ExternalStep):
        name = "ask"

        def validate(self, ctx, submission):
            return StepResult()

    cfg = tmp_path / "runspool.yaml"
    cfg.write_text(
        f"workspace_root: {tmp_path / 'ws'}\nscheduler:\n  max_retries: 0\n"
        "workflows:\n  w:\n    steps: [ask]\n",
        encoding="utf-8",
    )
    ctx = load_context(cfg)
    reg = StepRegistry()
    reg.register(Ask())
    tid = ctx.repo.create_task(input="x", workflow="w", first_step="ask", max_retries=0)
    ctx.state_machine("w").claim(tid, worker="t")
    TaskRunner(ctx.repo, ctx.log, ctx.step_runs, reg, ctx.config, notifier=lambda m: None).execute(
        tid
    )
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "has no work items" in task["last_error"]


def test_submission_file_resolves_relative_paths_and_checks_the_hash(tmp_path):
    root = tmp_path / "工作 区"
    (root / "子 目录").mkdir(parents=True)
    target = root / "子 目录" / "稿.md"
    target.write_text("内容", encoding="utf-8")
    sha = hashlib.sha256(target.read_bytes()).hexdigest()
    sub = Submission(1, 1, {"path": "子 目录/稿.md", "sha256": sha.upper()})
    assert sub.file(root) == target
    for value, reason in [
        ({}, "has no file path"),
        ({"path": "missing.md"}, "does not exist"),
        ({"path": "子 目录/稿.md", "sha256": ""}, "malformed"),
        ({"path": "子 目录/稿.md", "sha256": "0" * 64}, "does not match"),
    ]:
        with pytest.raises(SubmissionRejected, match=reason) as caught:
            Submission(1, 1, value).file(root)
        assert "稿.md" not in caught.value.reason


# -- the command line --------------------------------------------------------------


def test_the_items_command_lists_shows_claims_and_submits(ws, tmp_path):
    ctx, path, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    runner = CliRunner()

    def cli(*args, code=0):
        result = runner.invoke(app, ["-c", str(path), *args])
        assert result.exit_code == code, result.output
        return result.output

    listed = json.loads(cli("items", "--json"))
    assert [(i["task_id"], i["step"], i["status"]) for i in listed] == [(tid, "review", "open")]
    assert "claim_token" not in listed[0] and "payload" not in listed[0]
    item_id = listed[0]["id"]
    shown = json.loads(cli("items", "show", str(item_id), "--json"))
    assert shown["instructions"] == "校对「笔记」" and shown["submission"] is None
    assert "校对「笔记」" in cli("items", "show", str(item_id))
    cli("items", "show", "999", code=1)

    claimed = json.loads(cli("items", "claim", str(item_id), "--by", "人工", "--json"))
    token = claimed["claim_token"]
    assert "claimed by" not in cli("items", "claim", str(item_id), code=1)
    cli("items", "release", str(item_id), code=1)  # neither --token nor --force
    cli("items", "release", str(item_id), "--force")
    token = json.loads(cli("items", "claim", str(item_id), "--json"))["claim_token"]
    out_file, _ = _file(tmp_path, "通过", name="人工 结果.md")
    cli("items", "submit", str(item_id), "--token", token, code=1)  # nothing to submit
    cli("items", "submit", str(item_id), "--token", token, "--file", str(out_file))
    shown = json.loads(cli("items", "show", str(item_id), "--json"))
    assert shown["status"] == "submitted" and shown["submission"]["path"] == str(out_file)
    assert shown["submission"]["sha256"] == hashlib.sha256(out_file.read_bytes()).hexdigest()

    cli("run")
    assert json.loads(cli("items", "--json")) == []
    everything = json.loads(cli("items", "--all", "--task", str(tid), "--json"))
    assert [i["status"] for i in everything] == ["accepted"]
    assert _status(ctx, tid) == TaskStatus.COMPLETED


def test_items_value_and_json_submissions(ws):
    ctx, path, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    item_id = ctx.items.active_for(tid)["id"]
    runner = CliRunner()

    def cli(*args, code=0):
        result = runner.invoke(app, ["-c", str(path), *args])
        assert result.exit_code == code, result.output
        return result.output

    token = json.loads(cli("items", "claim", str(item_id), "--json"))["claim_token"]
    cli("items", "submit", str(item_id), "--token", token, "--json-value", "not json", code=1)
    cli("items", "submit", str(item_id), "--token", token, "--json-value", "[1]", code=1)
    cli("items", "submit", str(item_id), "--token", token, "--value", "一句话")
    assert ctx.items.get(item_id)["submission"] == {"value": "一句话"}


def test_items_lists_nothing_when_no_step_is_external(tmp_path):
    cfg = tmp_path / "config.yaml"
    runner = CliRunner()
    assert runner.invoke(app, ["-c", str(cfg), "init", "--workspace-root", str(tmp_path / "ws")])
    src = tmp_path / "笔记.txt"
    src.write_text("x", encoding="utf-8")
    for args in (["add", str(src)], ["run"]):
        assert runner.invoke(app, ["-c", str(cfg), *args]).exit_code == 0
    result = runner.invoke(app, ["-c", str(cfg), "items", "--all", "--json"])
    assert result.exit_code == 0 and json.loads(result.output) == []
    assert "no work items" in runner.invoke(app, ["-c", str(cfg), "items", "--all"]).output


def test_a_plugin_command_named_items_replaces_the_built_in_one(tmp_path, monkeypatch):
    module = "items_command_plugin"
    (tmp_path / f"{module}.py").write_text(
        textwrap.dedent(
            """
            import typer

            def plugin(ctx, config):
                ctx.cli.register(lambda: typer.echo("plugin items"), name="items")

            plugin.inject = ["cli"]
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, module, raising=False)
    cfg = tmp_path / "runspool.yaml"
    cfg.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        f"patch:\n  - insert:\n      - {{id: mine, plugin: '{module}:plugin'}}\n",
        encoding="utf-8",
    )
    from runspool.cli import build_app

    result = CliRunner().invoke(build_app(cfg), ["-c", str(cfg), "items"])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == "plugin items"


# -- a task that finishes without its item -------------------------------------------


@pytest.mark.parametrize(
    "workflow, final",
    [("tail", TaskStatus.COMPLETED), ("tail_degraded", TaskStatus.PARTIALLY_COMPLETED)],
)
def test_skipping_a_last_external_step_cancels_its_item(ws, tmp_path, workflow, final):
    ctx, _, module = ws
    tid = commands.add_task(ctx, "稿件 尾", workflow=workflow)
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(tid)
    assert item["step"] == "last_review"
    _submit(ctx, item["id"], *_file(tmp_path, "good"))
    sys.modules[module].STATE["run_last"] = False  # its condition no longer holds
    run_until_idle(ctx, notifier=lambda m: None)
    assert _status(ctx, tid) == final
    assert ctx.items.get(item["id"])["status"] == "cancelled"
    assert ctx.items.active_for(tid) is None
    assert ctx.items.list_items(statuses=("open", "claimed", "submitted")) == []


def test_accepting_the_last_external_step_finishes_with_the_item_accepted(ws, tmp_path):
    ctx, _, _ = ws
    tid = commands.add_task(ctx, "稿件 尾", workflow="tail")
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(tid)
    _submit(ctx, item["id"], *_file(tmp_path, "good"))
    run_until_idle(ctx, notifier=lambda m: None)
    assert _status(ctx, tid) == TaskStatus.COMPLETED
    assert ctx.items.get(item["id"])["status"] == "accepted"


# -- file system errors never put the submitted path anywhere ------------------------


def _everything_said(ctx, tid, notes):
    task = ctx.repo.get_task(tid)
    said = [task["last_error"] or ""]
    events = ctx.log.list_for_task(tid, limit=100)
    said += [f"{e['message']} {e.get('payload_json')}" for e in events]
    said += [f"{r['error']} {r['note']}" for r in ctx.step_runs.list_for_task(tid)]
    said += [f"{i['error']} {i['previous_error']}" for i in ctx.items.list_items(task_id=tid)]
    return "\n".join(said + notes)


def test_a_file_that_cannot_be_read_is_rejected_without_its_path(ws, tmp_path, monkeypatch):
    import runspool.engine.external as external

    ctx, _, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(tid)
    path, sha = _file(tmp_path, "good", name="秘密 文件.md")
    _submit(ctx, item["id"], path, sha)

    def denied(p):
        raise PermissionError(13, "Permission denied", str(p))

    monkeypatch.setattr(external, "sha256_of", denied)
    notes: list[str] = []
    run_until_idle(ctx, notifier=notes.append)
    said = _everything_said(ctx, tid, notes)
    assert "秘密 文件" not in said  # not in last_error, events, step runs, items, notices
    rejected = ctx.items.get(item["id"])
    assert rejected["status"] == "rejected"
    assert rejected["error"] == "the submitted file cannot be read (PermissionError)"
    assert ctx.items.active_for(tid)["previous_error"] == rejected["error"]


def test_a_file_that_disappears_after_the_check_is_rejected_without_its_path(tmp_path, monkeypatch):
    import runspool.engine.external as external

    target = tmp_path / "消失 的.md"
    target.write_text("x", encoding="utf-8")
    real = external.sha256_of

    def vanish(p):
        p.unlink()  # gone between the existence check and the read
        return real(p)

    monkeypatch.setattr(external, "sha256_of", vanish)
    sub = Submission(1, 1, {"path": str(target), "sha256": "0" * 64})
    with pytest.raises(SubmissionRejected) as caught:
        sub.file()
    assert caught.value.reason == "the submitted file cannot be read (FileNotFoundError)"
    assert "消失" not in caught.value.reason


# -- the command line never prints a submitted path from a file error ---------------

_CLI_WITH_FAULT = """
import sys

import runspool.engine.external as external

mode = sys.argv.pop(1)
real = external.sha256_of


def faulty(path):
    if mode == "denied":
        raise PermissionError(13, "Permission denied", str(path))
    path.unlink()  # gone between the existence check and the read
    return real(path)


external.sha256_of = faulty
from runspool.cli import main

sys.argv[0] = "runspool"
main()
"""


@pytest.mark.parametrize(
    "mode, kind", [("denied", "PermissionError"), ("vanished", "FileNotFoundError")]
)
def test_items_submit_reports_a_file_error_without_the_path(ws, tmp_path, mode, kind):
    import os
    import subprocess

    ctx, path, _ = ws
    tid = _new_task(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(tid)
    token = commands.claim_item(ctx, item["id"], by="agent")
    target, _ = _file(tmp_path, "good", name="私密 稿件.md")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _CLI_WITH_FAULT,
            mode,
            "-c",
            str(path),
            "items",
            "submit",
            str(item["id"]),
            "--token",
            token,
            "--file",
            str(target),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "私密 稿件" not in output and str(tmp_path) not in output
    assert "Traceback" not in output
    assert kind in output
    assert ctx.items.get(item["id"])["status"] == "claimed"  # nothing was submitted
    assert _status(ctx, tid) == TaskStatus.AWAITING_EXTERNAL


def test_a_submission_waits_while_the_steps_quota_is_full(ws, tmp_path):
    """Validation happens when the task can next be scheduled, not within a fixed
    time: while another validation of the same step holds its only slot, a second
    submission is not checked, however many rounds the daemon runs."""
    ctx, _, module = ws
    first, second = _new_task(ctx), commands.add_task(ctx, "稿件 二", workflow="drafts")
    run_until_idle(ctx, notifier=lambda m: None)
    entered, go = threading.Event(), threading.Event()
    sys.modules[module].STATE["hold"] = (entered, go)
    _submit(ctx, ctx.items.active_for(first)["id"], *_file(tmp_path, "good", name="1.md"))
    daemon = ctx.service("runtime").build_daemon()
    try:
        daemon.run_once()  # the first validation starts and holds the slot
        assert entered.wait(10)
        second_item = ctx.items.active_for(second)
        _submit(ctx, second_item["id"], *_file(tmp_path, "good", name="2.md"))
        for _ in range(3):
            daemon.run_once()
        assert _status(ctx, second) == TaskStatus.QUEUED  # submitted, not yet checked
        assert ctx.items.get(second_item["id"])["status"] == "submitted"
        assert [r["step"] for r in ctx.step_runs.list_for_task(second)] == ["draft"]
        sys.modules[module].STATE["hold"] = None
        go.set()
        for _ in range(6):
            daemon.run_once()
            daemon.coordinator.pool.drain()
        assert _status(ctx, first) == _status(ctx, second) == TaskStatus.COMPLETED
    finally:
        go.set()
        daemon.coordinator.pool.shutdown(wait=True)
