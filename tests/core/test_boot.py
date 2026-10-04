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
        "credentials",
        "approval",
        "cli",
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


# -- regressions from the M2 review ---------------------------------------------


NOISY_STEPS = """
print("IMPORTED custom step module")

from runspool.engine.step import Step, StepResult


class ArchiveClash(Step):
    name = "archive"

    def run(self, ctx):
        return StepResult()


class Quiet(Step):
    name = "quiet"

    def run(self, ctx):
        return StepResult()
"""


def noisy_profile(tmp_path, monkeypatch, steps_yaml: str, workflows_yaml: str):
    (tmp_path / "noisy_steps.py").write_text(NOISY_STEPS, encoding="utf-8")
    monkeypatch.delitem(__import__("sys").modules, "noisy_steps", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        f"workspace_root: {tmp_path / 'ws'}\nplugin_paths: ['.']\n"
        + textwrap.dedent(steps_yaml)
        + textwrap.dedent(workflows_yaml),
        encoding="utf-8",
    )
    return path


def test_a_custom_step_clashing_with_a_builtin_blocks_run(tmp_path, monkeypatch):
    path = noisy_profile(
        tmp_path,
        monkeypatch,
        """
        steps:
          archive:
            import: "noisy_steps:ArchiveClash"
        """,
        """
        workflows:
          w:
            steps: [archive]
        """,
    )
    ctx = load_context(path)
    with pytest.raises(StartupError, match="config-steps"):
        run_until_idle(ctx)  # 0.1 refused too; it must never run the built-in instead


def test_a_misnamed_custom_step_blocks_run_and_shows_in_doctor(tmp_path, monkeypatch):
    path = noisy_profile(
        tmp_path,
        monkeypatch,
        """
        steps:
          other:
            import: "noisy_steps:Quiet"
        """,
        """
        workflows:
          w:
            steps: [other]
        """,
    )
    ctx = load_context(path)
    with pytest.raises(StartupError, match="does not match"):
        run_until_idle(ctx)
    steps_check = next(c for c in ctx.service("doctor").run() if c.name == "steps")
    assert not steps_check.ok and "does not match" in steps_check.detail


def test_read_only_commands_do_not_import_step_code(tmp_path, monkeypatch):
    import json

    from typer.testing import CliRunner

    from runspool.cli import app

    path = noisy_profile(
        tmp_path,
        monkeypatch,
        """
        steps:
          quiet:
            import: "noisy_steps:Quiet"
        """,
        """
        workflows:
          w:
            steps: [quiet]
        """,
    )
    runner = CliRunner()
    for args in (["status", "--json"], ["overview", "--json"], ["workflows", "--json"]):
        result = runner.invoke(app, ["-c", str(path), *args])
        assert result.exit_code == 0, result.output
        json.loads(result.output)  # stdout stays a clean JSON document
        assert "IMPORTED" not in result.output
    assert "noisy_steps" not in __import__("sys").modules


@pytest.mark.parametrize("entry_id", ["tasks", "steps", "runtime", "workflows", "doctor"])
def test_core_entries_cannot_be_disabled(tmp_path, entry_id):
    path = write_profile(tmp_path, f"patch:\n  - id: {entry_id}\n    disabled: true\n")
    with pytest.raises(StartupError, match="cannot be turned off"):
        boot(path)


def test_a_profile_without_the_core_bundle_is_rejected(tmp_path):
    path = write_profile(tmp_path, "bundles: [builtin-steps]\n")
    with pytest.raises(StartupError, match="core"):
        boot(path)


def test_the_store_seam_can_be_replaced(tmp_path, monkeypatch):
    (tmp_path / "other_store.py").write_text(
        textwrap.dedent(
            """
            from runspool.core.store import Store
            from runspool.persistence.connection import Database

            def plugin(ctx, config):
                db = Database(ctx.config.workspace_root / "other.db")
                db.init()
                ctx.provide("store", Store(db))

            plugin.inject = ["config"]
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(__import__("sys").modules, "other_store", raising=False)
    path = write_profile(
        tmp_path,
        """
        patch:
          - id: store
            disabled: true
          - insert:
              - {id: other-store, plugin: "other_store:plugin"}
        """,
    )
    booted = boot(path)
    assert str(booted.service("store").db.path).endswith("other.db")


def test_unknown_settings_and_patch_warnings_show_in_doctor(tmp_path):
    path = write_profile(
        tmp_path,
        """
        patches: []
        patch:
          - id: builtin-archiv
            disabled: true
        """,
    )
    ctx = load_context(path)
    profile_check = next(c for c in ctx.service("doctor").run() if c.name == "profile")
    assert not profile_check.ok
    assert "'patches'" in profile_check.detail
    assert "builtin-archiv" in profile_check.detail


def test_init_reports_a_malformed_existing_profile_cleanly(tmp_path):
    from typer.testing import CliRunner

    from runspool.cli import app

    path = write_profile(tmp_path, "bundles: core\n")
    result = CliRunner().invoke(app, ["-c", str(path), "init"])
    assert result.exit_code == 1
    assert "bundles must be a list" in result.output
    assert "Traceback" not in result.output


def test_the_tasks_service_runs_every_user_action(tmp_path):
    # Regression: the service handed the command functions a context without
    # state_machine(), so pause/resume/retry/terminate raised AttributeError.
    ctx = load_context(write_profile(tmp_path, ""))
    tasks = ctx.service("tasks")
    tid = tasks.add("in.txt", workflow="local_file")
    tasks.pause(tid)
    assert ctx.repo.get_task(tid)["task_status"] == "paused"
    tasks.resume(tid)
    tasks.terminate(tid)
    assert ctx.repo.get_task(tid)["task_status"] == "terminated"


def test_relative_paths_resolve_against_the_profile_not_the_cwd(tmp_path, monkeypatch):
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.txt").write_text("hello", encoding="utf-8")
    profile = project / "runspool.yaml"
    profile.write_text("workspace_root: ./workspace\n", encoding="utf-8")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    ctx = load_context(profile)
    assert ctx.config.workspace_root == project / "workspace"
    monkeypatch.chdir(project)
    tid = ctx.service("tasks").add("a.txt", workflow="local_file")
    assert ctx.repo.get_task(tid)["input"] == str(project / "a.txt")  # stored absolute
