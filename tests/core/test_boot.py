"""Booting RunSpool from a profile: core plugins, bundles, patches, failures."""

from __future__ import annotations

import textwrap

import pytest

from runspool.app import load_context
from runspool.core.boot import boot
from runspool.kernel import FiberState, StartupError
from runspool.runtime import run_until_idle

CUSTOM_STEPS = """
from runspool.engine.step import Step, StepResult
from runspool.kernel import Plugin


class Greet(Step):
    name = "greet"

    def run(self, ctx):
        return StepResult(message="hello")


class MyArchive(Step):
    name = "archive"

    def run(self, ctx):
        return StepResult(message="my archive")


class Clash(Step):
    name = "archive"

    def run(self, ctx):
        return StepResult()


def replace_archive(ctx, config):
    ctx.steps.register(MyArchive())


replace_archive.inject = ["steps"]


def contribute(ctx, config):
    ctx.workflows.add_default("greeting", ["greet"])
    ctx.workflows.add_default("local_file", ["greet"])  # the profile's own wins
    ctx.doctor.register(lambda: __import__("runspool.core.doctor").core.doctor.Check(
        "greeter", True, "fine"))
    ctx.steps.register(Greet())


contribute.inject = ["steps", "workflows", "doctor"]
"""


def write_profile(tmp_path, body: str, monkeypatch=None):
    (tmp_path / "custom_steps.py").write_text(CUSTOM_STEPS, encoding="utf-8")
    if monkeypatch is not None:
        monkeypatch.syspath_prepend(str(tmp_path))
        monkeypatch.delitem(__import__("sys").modules, "custom_steps", raising=False)
    path = tmp_path / "runspool.yaml"
    path.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n" + textwrap.dedent(body), encoding="utf-8"
    )
    return path


def test_a_01_style_config_boots_every_core_and_builtin_entry(tmp_path):
    booted = boot(write_profile(tmp_path, ""))
    states = {s.id: s.state for s in booted.report.entries}
    assert set(states) == {
        "store",
        "steps",
        "workflows",
        "tasks",
        "runtime",
        "doctor",
        "builtin-ingest_file",
        "builtin-classify_text",
        "builtin-normalize_markdown",
        "builtin-summarize_text",
        "builtin-archive",
        "config-steps",
    }
    assert set(states.values()) == {FiberState.ACTIVE.value}
    assert booted.service("steps").names() == [
        "archive",
        "classify_text",
        "ingest_file",
        "normalize_markdown",
        "summarize_text",
    ]


def test_profile_replaces_one_builtin_step(tmp_path, monkeypatch):
    path = write_profile(
        tmp_path,
        """
        patch:
          - id: builtin-archive
            disabled: true
          - insert:
              - {id: my-archive, plugin: "custom_steps:replace_archive"}
        """,
        monkeypatch,
    )
    booted = boot(path)
    archive = booted.service("steps").registry.get("archive")
    assert type(archive).__name__ == "MyArchive"


def test_plugins_contribute_workflows_steps_and_checks(tmp_path, monkeypatch):
    path = write_profile(
        tmp_path,
        """
        patch:
          - insert:
              - {id: greeter, plugin: "custom_steps:contribute"}
        """,
        monkeypatch,
    )
    ctx = load_context(path)
    assert ctx.config.workflows["greeting"].steps == ["greet"]
    assert ctx.config.workflows["local_file"].steps[0] == "ingest_file"
    checks = {c.name: c for c in ctx.service("doctor").run()}
    assert checks["greeter"].ok
    assert checks["plugins"].ok

    task_id = ctx.service("tasks").add("hi", workflow="greeting")
    run_until_idle(ctx, notifier=lambda m: None)
    assert ctx.repo.get_task(task_id)["task_status"] == "completed"


def test_a_clashing_config_step_fails_its_plugin_but_not_startup(tmp_path, monkeypatch):
    path = write_profile(
        tmp_path,
        """
        steps:
          archive:
            import: "custom_steps:Clash"
        """,
        monkeypatch,
    )
    ctx = load_context(path)  # status, logs, etc. keep working
    states = {s.id: s.state for s in ctx.service("startup").report().entries}
    assert states["config-steps"] == "failed"
    assert states["builtin-archive"] == "active"  # the built-in registered first, as in 0.1


def test_missing_steps_caused_by_a_failed_plugin_stop_run_and_show_in_doctor(tmp_path, monkeypatch):
    path = write_profile(
        tmp_path,
        """
        plugin_paths: ["."]
        steps:
          greet:
            import: "custom_steps:NoSuchStep"
        workflows:
          greeting:
            steps: [greet]
        """,
        monkeypatch,
    )
    ctx = load_context(path)
    with pytest.raises(StartupError, match="greet"):
        run_until_idle(ctx)
    steps_check = next(c for c in ctx.service("doctor").run() if c.name == "steps")
    assert not steps_check.ok
    assert "plugin load failed" in steps_check.detail
    assert "NoSuchStep" in steps_check.detail


def test_a_required_entry_that_fails_stops_startup(tmp_path, monkeypatch):
    path = write_profile(
        tmp_path,
        """
        required: [broken]
        patch:
          - insert:
              - {id: broken, plugin: "custom_steps:NoSuchPlugin"}
        """,
        monkeypatch,
    )
    with pytest.raises(StartupError, match="broken"):
        boot(path)


def test_disabling_a_core_entry_is_reported_not_hidden(tmp_path):
    path = write_profile(
        tmp_path,
        """
        patch:
          - id: store
            disabled: true
        """,
    )
    with pytest.raises(StartupError, match="waiting for store"):
        boot(path)


def test_cli_reports_startup_failures_without_a_traceback(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from runspool.cli import app

    path = write_profile(
        tmp_path,
        """
        plugin_paths: ["."]
        steps:
          greet:
            import: "custom_steps:NoSuchStep"
        workflows:
          greeting:
            steps: [greet]
        """,
        monkeypatch,
    )
    runner = CliRunner()
    assert runner.invoke(app, ["-c", str(path), "status"]).exit_code == 0
    result = runner.invoke(app, ["-c", str(path), "run"])
    assert result.exit_code == 1
    assert "NoSuchStep" in result.output
    assert "Traceback" not in result.output
