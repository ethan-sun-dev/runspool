"""The plugin mounted in RunSpool: render, wait for approval, draft."""

from __future__ import annotations

import json

import pytest
import runspool_wechat.steps as steps_module
from typer.testing import CliRunner

from runspool.app import load_context
from runspool.cli import build_app
from runspool.models import TaskStatus
from runspool.runtime import run_until_idle

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 64


class FakeClient:
    instances: list[FakeClient] = []

    def __init__(self, appid, secret):
        self.appid, self.secret = appid, secret
        self.uploads, self.drafts, self.updates = [], [], []
        FakeClient.instances.append(self)

    def access_token(self):
        return "TOKEN", 7000

    def upload_content_image(self, path):
        self.uploads.append(path.name)
        return f"https://mmbiz.qpic.cn/{path.name}"

    def add_image_material(self, path):
        return "COVER"

    def add_draft(self, draft):
        self.drafts.append(draft)
        return "DRAFT-1"

    def update_draft(self, media_id, draft):
        self.updates.append((media_id, draft))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    FakeClient.instances.clear()
    monkeypatch.setattr(steps_module, "make_client", FakeClient)
    monkeypatch.setenv("WECHAT_APPID", "wx-test")
    monkeypatch.setenv("WECHAT_APPSECRET", "s3cr3t-value")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    article_dir = tmp_path / "post"
    (article_dir / "img").mkdir(parents=True)
    (article_dir / "img" / "chart.png").write_bytes(PNG)
    (article_dir / "cover.png").write_bytes(PNG)
    article = article_dir / "wechat.md"
    article.write_text(
        "# A short title\n\nIntro with a [link](https://example.com).\n\n![Chart](img/chart.png)\n",
        encoding="utf-8",
    )
    profile = tmp_path / "runspool.yaml"
    profile.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        "bundles: [core, builtin-steps, wechat]\n"
        "patch:\n  - id: wechat\n    config: {author: Ethan}\n",
        encoding="utf-8",
    )
    return load_context(profile), article, profile


def run(ctx):
    run_until_idle(ctx, notifier=lambda m: None)


def test_render_then_wait_for_approval_then_draft(setup):
    ctx, article, _ = setup
    tasks = ctx.service("tasks")
    tid = tasks.add(str(article), workflow="wechat_article")
    run(ctx)
    task = ctx.repo.get_task(tid)
    assert (task["task_status"], task["step"], task["name"]) == (
        TaskStatus.AWAITING_APPROVAL,
        "wechat_draft",
        "A short title",
    )
    assert FakeClient.instances == []  # nothing reached WeChat before approval

    tasks.approve(tid, by="ethan")
    run(ctx)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.COMPLETED
    assert task["metadata"]["wechat_media_id"] == "DRAFT-1"
    client = FakeClient.instances[-1]
    assert (client.appid, client.uploads) == ("wx-test", ["chart.png"])
    draft = client.drafts[0]
    assert (draft["title"], draft["author"], draft["thumb_media_id"]) == (
        "A short title",
        "Ethan",
        "COVER",
    )
    assert "https://mmbiz.qpic.cn/chart.png" in draft["content"]
    # The secret appears in no event, error or note.
    trail = json.dumps(ctx.log.list_for_task(tid)) + json.dumps(ctx.step_runs.list_for_task(tid))
    assert "s3cr3t-value" not in trail


def test_a_retried_draft_reuses_its_uploads(setup):
    ctx, article, _ = setup
    tasks = ctx.service("tasks")
    tid = tasks.add(str(article), workflow="wechat_article")
    run(ctx)
    tasks.approve(tid, by="ethan")
    real_add = FakeClient.add_draft
    calls = {"n": 0}

    def flaky_add(self, draft):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("platform hiccup")
        return real_add(self, draft)

    FakeClient.add_draft = flaky_add
    try:
        run(ctx)  # first attempt fails after uploading; the retry asks again
        assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.AWAITING_APPROVAL
        tasks.approve(tid, by="ethan")
        run(ctx)
    finally:
        FakeClient.add_draft = real_add
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.COMPLETED
    first, second = FakeClient.instances[-2:]
    assert (first.uploads, second.uploads) == (["chart.png"], [])  # cached the second time


def test_a_known_draft_is_updated_not_duplicated(setup):
    ctx, article, _ = setup
    tasks = ctx.service("tasks")
    tid = tasks.add(str(article), workflow="wechat_article", metadata={"wechat_media_id": "OLD"})
    run(ctx)
    tasks.approve(tid, by="ethan")
    run(ctx)
    client = FakeClient.instances[-1]
    assert client.drafts == [] and client.updates[0][0] == "OLD"
    assert ctx.repo.get_task(tid)["metadata"]["wechat_media_id"] == "OLD"


def test_problems_hold_the_draft_back_without_asking_anyone(setup):
    ctx, article, _ = setup
    article.write_text("# " + "长" * 40 + "\n\n【配图：todo】\n", encoding="utf-8")
    tasks = ctx.service("tasks")
    tid = tasks.add(str(article), workflow="wechat_article")
    run(ctx)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "title has 40 characters" in task["last_error"]
    assert "approval_asked" not in [e["event_type"] for e in ctx.log.list_for_task(tid)]
    render_run = ctx.step_runs.list_for_task(tid)[0]
    assert render_run["status"] == "degraded"


def test_missing_credentials_are_named_not_shown(setup, monkeypatch):
    ctx, article, profile = setup
    monkeypatch.delenv("WECHAT_APPSECRET")
    checks = {c.name: c for c in ctx.service("doctor").run()}
    assert not checks["wechat credentials"].ok
    assert "WECHAT_APPSECRET" in checks["wechat credentials"].detail


def test_the_commands(setup):
    ctx, article, profile = setup
    cli = CliRunner()
    token = cli.invoke(build_app(profile), ["-c", str(profile), "wechat", "token"])
    assert token.exit_code == 0, token.output
    assert "TOKEN" not in token.output and "valid for 7000s" in token.output
    out = article.parent / "preview.html"
    preview = cli.invoke(
        build_app(profile), ["-c", str(profile), "wechat", "preview", str(article), "-o", str(out)]
    )
    assert preview.exit_code == 0, preview.output
    assert "A short title" in out.read_text(encoding="utf-8")


# -- regressions from the M5 review ---------------------------------------------------


def test_an_article_edited_after_rendering_is_not_drafted(setup):
    ctx, article, _ = setup
    tasks = ctx.service("tasks")
    tid = tasks.add(str(article), workflow="wechat_article")
    run(ctx)  # rendered, awaiting approval
    article.write_text("# " + "长" * 40 + "\n\n![n](img/new.png)\n", encoding="utf-8")
    tasks.approve(tid, by="ethan")
    run(ctx)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "changed since it was rendered" in task["last_error"]
    assert all(not c.drafts for c in FakeClient.instances)


def test_the_approval_request_lists_the_files_to_upload(setup):
    ctx, article, _ = setup
    tid = ctx.service("tasks").add(str(article), workflow="wechat_article")
    run(ctx)
    asked = next(e for e in ctx.log.list_for_task(tid) if e["event_type"] == "approval_asked")
    assert "chart.png" in asked["message"] and "cover.png" in asked["message"]


def test_a_draft_deleted_in_the_back_end_is_recreated(setup):
    from runspool_wechat.api import WeChatError

    ctx, article, _ = setup

    def gone(self, media_id, draft):
        raise WeChatError("draft/update", "error 40007: invalid media_id", errcode=40007)

    FakeClient.update_draft, real = gone, FakeClient.update_draft
    try:
        tasks = ctx.service("tasks")
        tid = tasks.add(
            str(article), workflow="wechat_article", metadata={"wechat_media_id": "GONE"}
        )
        run(ctx)
        tasks.approve(tid, by="ethan")
        run(ctx)
    finally:
        FakeClient.update_draft = real
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.COMPLETED
    assert task["metadata"]["wechat_media_id"] == "DRAFT-1"


def test_uploads_stay_inside_the_article_directory(setup):
    ctx, article, _ = setup
    (article.parent.parent / "secret.png").write_bytes(PNG)
    article.write_text("# Title\n\n![x](../secret.png)\n", encoding="utf-8")
    tasks = ctx.service("tasks")
    tid = tasks.add(str(article), workflow="wechat_article")
    run(ctx)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "outside the article's directory" in task["last_error"]


def test_a_secret_pasted_as_a_credential_name_is_refused_and_never_echoed(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    secret = "abcdef0123456789abcdef0123456789"
    profile = tmp_path / "runspool.yaml"
    profile.write_text(
        f"workspace_root: {tmp_path / 'ws'}\nbundles: [core, builtin-steps, wechat]\n"
        f"patch:\n  - id: wechat\n    config: {{appsecret: {secret}}}\n",
        encoding="utf-8",
    )
    ctx = load_context(profile)
    report = " ".join(ctx.service("startup").report().lines())
    assert "appsecret" in report and secret not in report


def test_preview_reports_errors_cleanly(setup, tmp_path):
    ctx, article, profile = setup
    cli = CliRunner()
    missing = cli.invoke(
        build_app(profile), ["-c", str(profile), "wechat", "preview", str(tmp_path / "nope.md")]
    )
    assert missing.exit_code == 1 and "Traceback" not in missing.output
    assert "error:" in missing.output
    nested = tmp_path / "a" / "b" / "out.html"
    ok = cli.invoke(
        build_app(profile),
        ["-c", str(profile), "wechat", "preview", str(article), "-o", str(nested)],
    )
    assert ok.exit_code == 0 and nested.exists()
