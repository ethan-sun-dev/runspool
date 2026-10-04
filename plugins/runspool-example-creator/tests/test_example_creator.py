"""The example plugin, mounted by its entry-point name, runs its workflow end to end."""

from __future__ import annotations

from pathlib import Path

from runspool.app import load_context
from runspool.runtime import run_until_idle

MATERIALS = Path(__file__).resolve().parents[3] / "examples/creator-publishing-pipeline/materials"


def test_the_plugin_builds_a_draft_package(tmp_path):
    profile = tmp_path / "runspool.yaml"
    profile.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        "patch:\n  - insert: [{id: creator, plugin: example-creator}]\n",
        encoding="utf-8",
    )
    ctx = load_context(profile)
    assert ctx.config.workflows["creator_publishing"].steps[-1] == "archive"
    tid = ctx.service("tasks").add(str(MATERIALS), workflow="creator_publishing", name="Demo")
    run_until_idle(ctx, notifier=lambda m: None)
    assert ctx.repo.get_task(tid)["task_status"] == "completed"
    dist = tmp_path / "ws" / "ready" / str(tid) / "dist"
    assert {"article.md", "wechat.html", "publish-checklist.md"} <= {p.name for p in dist.iterdir()}
