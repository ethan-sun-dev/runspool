"""Artifact contracts are opt-in: without declarations nothing changes."""

from __future__ import annotations

import json

from typer.testing import CliRunner

from runspool.cli import app
from runspool.engine.step import Step, StepResult

runner = CliRunner()

# What doctor and inspect showed before artifact contracts existed.
DOCTOR_CHECKS = [
    "python",
    "workspace_root",
    "database",
    "workflows",
    "steps",
    "plugins",
    "external",
    "profile",
    "credentials file",
]
INSPECT_KEYS = [
    "id",
    "name",
    "input",
    "parent_task_id",
    "metadata",
    "status",
    "workflow",
    "current_step",
    "priority",
    "retry_count",
    "max_retries",
    "progress",
    "last_error",
    "recent_events",
    "step_runs",
    "artifacts",
    "next_retry_at",
    "available_actions",
    "suggested_next_action",
]


def _cli(cfg, *args):
    result = runner.invoke(app, ["-c", str(cfg), *args])
    assert result.exit_code == 0, result.output
    return result.output


def test_the_built_in_workflow_runs_as_before_and_records_nothing(tmp_path):
    cfg = tmp_path / "config.yaml"
    _cli(cfg, "init", "--workspace-root", str(tmp_path / "工作 区"))
    src = tmp_path / "笔记 一.txt"
    src.write_text("第一篇笔记。Runspool keeps state local.", encoding="utf-8")
    _cli(cfg, "add", str(src))
    _cli(cfg, "run")

    view = json.loads(_cli(cfg, "inspect", "1", "--json"))
    assert list(view) == INSPECT_KEYS
    assert view["status"] == "completed"
    assert all(isinstance(a, str) for a in view["artifacts"])  # still the file listing
    assert [c["name"] for c in json.loads(_cli(cfg, "doctor", "--json"))] == DOCTOR_CHECKS
    assert json.loads(_cli(cfg, "artifacts", "1", "--json")) == []


def test_defaults_declare_nothing_and_record_nothing():
    class Plain(Step):
        name = "plain"

        def run(self, ctx):
            return StepResult()

    assert Plain.requires is None and Plain.provides is None
    assert StepResult().artifacts == ()
