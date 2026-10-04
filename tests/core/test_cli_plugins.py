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
