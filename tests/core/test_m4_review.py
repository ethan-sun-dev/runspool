"""Regressions from the M4 review: approval cannot be forged, outlived or misattributed;
credentials never leak; the CLI and migrations hold up."""

from __future__ import annotations

import json
import multiprocessing
import sqlite3
import sys

import pytest
from typer.testing import CliRunner

from runspool.app import load_context
from runspool.cli import _config_path_from, build_app
from runspool.core.credentials import LocalCredentials, parse_dotenv
from runspool.models import TaskStatus
from runspool.persistence.connection import Database
from runspool.persistence.schema import SCHEMA_VERSION
from runspool.runtime import run_until_idle
from tests.core.test_approval import STEPS as BASE_STEPS
from tests.persistence.test_migrations import V01_SCHEMA

TRICKS = """

from runspool.engine.gate import grant_value


def forge(ctx, config):
    def policy(req, next_):
        req.task["approval_grant"] = grant_value(req.step.name, req.attempt)
        return next_()
    ctx.on("step/pre-execute", policy)


def flip(ctx, config):
    def policy(req, next_):
        req.step.side_effect = False
        return next_()
    ctx.on("step/pre-execute", policy)


def robot(ctx, config):
    ctx.on("approval/request", lambda task: ctx.tasks.approve(task["id"], by="a human, honest"))

robot.inject = ["tasks"]
"""


@pytest.fixture
def make(tmp_path, monkeypatch):
    (tmp_path / "tricks.py").write_text(BASE_STEPS + TRICKS, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "tricks", raising=False)

    def build(*extra):
        inserts = [{"id": "release", "plugin": "tricks:register"}]
        inserts += [{"id": e, "plugin": f"tricks:{e}"} for e in extra]
        path = tmp_path / "runspool.yaml"
        path.write_text(
            f"workspace_root: {tmp_path / 'ws'}\npatch:\n  - insert: {json.dumps(inserts)}\n",
            encoding="utf-8",
        )
        ctx = load_context(path)
        sys.modules["tricks"].RUNS.clear()
        return ctx, sys.modules["tricks"]

    return build


def run(ctx):
    run_until_idle(ctx, notifier=lambda m: None)


@pytest.mark.parametrize("trick", ["forge", "flip"])
def test_a_pre_execute_listener_cannot_get_a_side_effect_past_approval(make, trick):
    ctx, mod = make(trick)
    ctx.service("tasks").add("v1", workflow="release")
    run(ctx)
    assert ctx.repo.get_task(1)["task_status"] in (
        TaskStatus.AWAITING_APPROVAL,
        TaskStatus.MANUAL_REQUIRED,  # a listener that mutates its request is refused
    )
    assert all(step != "publish" for step, _ in mod.RUNS)
    # And the registry's step is not changed for later tasks either.
    ctx.service("tasks").add("v2", workflow="release")
    run(ctx)
    assert all(step != "publish" for step, _ in mod.RUNS)


def test_a_plugin_approval_is_recorded_as_that_plugin(make):
    ctx, mod = make("robot")
    ctx.service("tasks").add("v1", workflow="release")
    run(ctx)
    decided = [e for e in ctx.log.list_for_task(1) if e["event_type"] == "approval_decided"]
    assert decided, "the robot approved"
    by = json.loads(decided[0]["payload_json"])["by"]
    assert by.startswith("plugin:robot")  # not the caller-supplied "a human, honest"


def test_set_step_clears_a_grant_and_cannot_move_a_waiting_task(make):
    ctx, mod = make()
    tasks = ctx.service("tasks")
    tid = tasks.add("v1", workflow="release")
    run(ctx)
    sm = ctx.state_machine("release")
    from runspool.persistence.state_machine import IllegalTransition

    with pytest.raises(IllegalTransition):
        sm.set_step(tid, "prepare", force=True)  # from awaiting_approval
    tasks.approve(tid, by="ethan")
    sm.set_step(tid, "prepare", force=True)  # rewind after approval
    assert ctx.repo.get_task(tid)["approval_grant"] is None
    run(ctx)
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.AWAITING_APPROVAL
    assert ("publish", 1) not in mod.RUNS


def test_a_guard_refuses_before_anyone_is_asked(make, tmp_path):
    ctx, mod = make()
    ctx.service("steps").guard(lambda req: "frozen" if req.step.name == "publish" else None)
    ctx.service("tasks").add("v1", workflow="release")
    run(ctx)
    task = ctx.repo.get_task(1)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED and "frozen" in task["last_error"]


# -- credentials ----------------------------------------------------------------------


def creds(tmp_path, user_text=None, profile_text=None):
    user = tmp_path / "credentials.yaml"
    profile = tmp_path / "profile.env"
    if user_text is not None:
        user.write_text(user_text, encoding="utf-8")
    if profile_text is not None:
        profile.write_text(profile_text, encoding="utf-8")
    return LocalCredentials(env={}, user_file=user, profile_env=profile, home_env=None)


def test_a_broken_credentials_file_never_leaks_a_value_and_falls_through(tmp_path):
    c = creds(tmp_path, 'WECHAT_APPSECRET: "s3cr3t-VALUE\n', "OTHER=fine\n")
    assert c.resolve("OTHER") == "fine"
    assert c.resolve("WECHAT_APPSECRET") is None
    problem = c.file_problem()
    assert problem and "s3cr3t" not in problem and "credentials.yaml" in problem
    assert "s3cr3t" not in c.check(["WECHAT_APPSECRET"]).detail


def test_credential_values_are_kept_verbatim(tmp_path):
    c = creds(tmp_path, "PIN: 0123\nFLAG: yes\nHEX: 0x1F\nZERO: 0\n")
    assert [c.resolve(n) for n in ("PIN", "FLAG", "HEX", "ZERO")] == ["0123", "yes", "0x1F", "0"]


def test_dotenv_inline_comments_and_tabs():
    text = 'A=abc # comment\nB="q # not a comment" # c\nexport\tE=1\nF=a#b\n'
    assert parse_dotenv(text) == {"A": "abc", "B": "q # not a comment", "E": "1", "F": "a#b"}


def test_relative_xdg_config_home_is_ignored(monkeypatch, tmp_path):
    from runspool.core.credentials import default_user_file

    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/dir")
    assert default_user_file().is_absolute()


# -- CLI -----------------------------------------------------------------------------


def test_disabling_the_cli_entry_keeps_built_in_commands(tmp_path):
    path = tmp_path / "runspool.yaml"
    path.write_text(
        f"workspace_root: {tmp_path / 'ws'}\npatch:\n  - id: cli\n    disabled: true\n",
        encoding="utf-8",
    )
    result = CliRunner().invoke(build_app(path), ["-c", str(path), "status"])
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("argv", [["-cb.yaml", "x"], ["-c", "b.yaml"], ["--config-path=b.yaml"]])
def test_attached_short_option_is_understood(argv):
    assert str(_config_path_from(argv)) == "b.yaml"


# -- migrations ----------------------------------------------------------------------


def _init(path):
    Database(path).init()


@pytest.mark.parametrize("wal", [True, False])
def test_concurrent_upgrades_of_an_old_database_all_succeed(tmp_path, wal):
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        if wal:
            conn.execute("pragma journal_mode = wal")
        conn.executescript(V01_SCHEMA)
    with multiprocessing.get_context("spawn").Pool(4) as pool:
        pool.map(_init, [path] * 40)  # raises if any init failed
    assert Database(path).init() == SCHEMA_VERSION
