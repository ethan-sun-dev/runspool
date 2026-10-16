"""Plugins contribute command-line subcommands."""

from __future__ import annotations

import sys

import pytest
from typer.testing import CliRunner

from runspool.cli import _config_path_from, build_app

COMMANDS = '''
import typer

from runspool.kernel import Plugin


def contribute(ctx, config):
    group = typer.Typer(name="wechat", help="WeChat sub-flows")

    @group.command("draft")
    def draft(parent: int):
        """Create a WeChat sub-flow for an archived task."""
        task_id = ctx.tasks.add(f"wechat:{parent}", workflow="local_file", parent=parent,
                                metadata={"source": parent})
        typer.echo(f"sub-flow {task_id} for {parent}")

    def hello(name: str = "world"):
        typer.echo(f"hello {name}")

    def status():  # clashes with the built-in `status`
        typer.echo("not mine")

    ctx.cli.register(group)
    ctx.cli.register(hello)
    ctx.cli.register(status)


contribute.inject = ["cli", "tasks"]
'''


@pytest.fixture
def profile(tmp_path, monkeypatch):
    (tmp_path / "my_commands.py").write_text(COMMANDS, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "my_commands", raising=False)
    path = tmp_path / "runspool.yaml"
    path.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        "patch:\n  - insert: [{id: mine, plugin: 'my_commands:contribute'}]\n",
        encoding="utf-8",
    )
    return path


def invoke(path, *args):
    return CliRunner().invoke(build_app(path), ["-c", str(path), *args])


def test_plugin_groups_and_commands_are_available(profile):
    result = invoke(profile, "hello", "--name", "ethan")
    assert result.exit_code == 0 and "hello ethan" in result.output
    src = profile.parent / "a.txt"
    src.write_text("x", encoding="utf-8")
    assert invoke(profile, "add", str(src)).exit_code == 0
    made = invoke(profile, "wechat", "draft", "1")
    assert made.exit_code == 0, made.output
    assert "sub-flow 2 for 1" in made.output
    child = invoke(profile, "inspect", "2", "--json").output
    assert '"parent_task_id": 1' in child


def test_a_plugin_cannot_replace_a_built_in_command(profile, capsys):
    cli = build_app(profile)
    assert "clashes with a built-in command" in capsys.readouterr().err
    result = CliRunner().invoke(cli, ["-c", str(profile), "status"])
    assert result.exit_code == 0
    assert "not mine" not in result.output


def test_built_in_commands_still_work_when_the_profile_does_not_boot(tmp_path):
    broken = tmp_path / "runspool.yaml"
    broken.write_text("bundles: core\n", encoding="utf-8")  # malformed
    result = invoke(broken, "status")
    assert result.exit_code == 1
    assert "bundles must be a list" in result.output


@pytest.mark.parametrize(
    "argv, expected",
    [
        (["status"], "config.yaml"),
        (["-c", "a.yaml", "status"], "a.yaml"),
        (["--config-path", "b.yaml"], "b.yaml"),
        (["--config-path=c.yaml", "run"], "c.yaml"),
    ],
)
def test_config_path_is_found_before_parsing(argv, expected):
    assert str(_config_path_from(argv)) == expected


LEGACY_ARTIFACTS = """
import typer


def contribute(ctx, config):
    def artifacts(task_id: int):  # a plugin's own command, from before RunSpool had one
        typer.echo(f"legacy artifacts of {task_id}")

    ctx.cli.register(artifacts)


contribute.inject = ["cli"]
"""


def test_a_plugin_command_named_artifacts_keeps_working(tmp_path, monkeypatch, capsys):
    """`artifacts` became a built-in command after 0.2.2; a plugin that already had
    one keeps it (the built-in yields), whether or not contracts are used."""
    (tmp_path / "legacy_commands.py").write_text(LEGACY_ARTIFACTS, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "legacy_commands", raising=False)
    path = tmp_path / "runspool.yaml"
    path.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        "patch:\n  - insert: [{id: legacy, plugin: 'legacy_commands:contribute'}]\n",
        encoding="utf-8",
    )
    result = invoke(path, "artifacts", "7")
    assert result.exit_code == 0, result.output
    assert "legacy artifacts of 7" in result.output
    assert "clashes" not in capsys.readouterr().err


def test_the_built_in_artifacts_command_is_there_without_such_a_plugin(tmp_path):
    path = tmp_path / "runspool.yaml"
    path.write_text(f"workspace_root: {tmp_path / 'ws'}\n", encoding="utf-8")
    result = invoke(path, "artifacts", "1", "--json")
    assert result.exit_code == 1 and '"not_found"' in result.output


# Built-in commands added in 0.3.0. A plugin may already have registered one of
# these names (under 0.2.2, where none was built in); its command keeps working.
NEWER_BUILTINS = ["artifacts", "items", "events", "plugins"]

LEGACY_COMMAND = """
import typer


def contribute(ctx, config):
    def legacy(task_id: int):
        \"\"\"The plugin's own {name} command.\"\"\"
        typer.echo(f"legacy {name} of {{task_id}}")

    ctx.cli.register(legacy, name="{name}")


contribute.inject = ["cli"]
"""


def _profile_with(tmp_path, monkeypatch, name):
    module = f"legacy_{name}_command"
    (tmp_path / f"{module}.py").write_text(LEGACY_COMMAND.format(name=name), encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, module, raising=False)
    path = tmp_path / "with-plugin.yaml"
    path.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        f"patch:\n  - insert: [{{id: legacy, plugin: '{module}:contribute'}}]\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("name", NEWER_BUILTINS)
def test_a_plugin_command_named_like_a_newer_built_in_is_used(tmp_path, monkeypatch, capsys, name):
    path = _profile_with(tmp_path, monkeypatch, name)
    result = invoke(path, name, "7")
    assert result.exit_code == 0, result.output
    assert result.output == f"legacy {name} of 7\n"
    assert "clashes" not in capsys.readouterr().err
    listed = invoke(path, "--help").output  # listed once, with the plugin's own help
    assert f"The plugin's own {name} command." in listed
    assert sum(line.split()[1:2] == [name] for line in listed.splitlines()) == 1


@pytest.mark.parametrize(
    "name, args, expected",
    [
        ("artifacts", ["1", "--json"], '"not_found"'),
        ("items", ["--json"], "[]"),
        ("events", ["--since", "0", "--json"], '"cursor": 0'),
        ("plugins", ["--json"], '"credentials"'),
    ],
)
def test_without_such_a_plugin_the_built_in_is_there(tmp_path, monkeypatch, name, args, expected):
    path = tmp_path / "no-plugin.yaml"
    path.write_text(f"workspace_root: {tmp_path / 'ws'}\n", encoding="utf-8")
    result = invoke(path, name, *args)
    assert expected in result.output, result.output
    assert "legacy" not in result.output
    # The plugin is mounted by another profile only: this one keeps the built-in.
    _profile_with(tmp_path, monkeypatch, name)
    assert expected in invoke(path, name, *args).output
