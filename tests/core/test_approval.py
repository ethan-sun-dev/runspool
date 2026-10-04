"""The approval gate: steps with side effects run only after a human approves."""

from __future__ import annotations

import json
import sys

import pytest
from typer.testing import CliRunner

from runspool.app import load_context
from runspool.cli import app
from runspool.engine.gate import Allow, Ask, GateRequest
from runspool.engine.registry import StepRegistry
from runspool.engine.runner import TaskRunner
from runspool.models import TaskStatus
from runspool.runtime import run_until_idle

STEPS = """
from runspool.engine.gate import Allow, Ask, Deny
from runspool.engine.step import Step, StepResult
from runspool.kernel import Plugin

RUNS = []


class Prepare(Step):
    name = "prepare"

    def run(self, ctx):
        RUNS.append(("prepare", ctx.attempt))
        return StepResult()


class Publish(Step):
    name = "publish"
    side_effect = True
    fail_times = 0

    def run(self, ctx):
        RUNS.append(("publish", ctx.attempt))
        if Publish.fail_times:
            Publish.fail_times -= 1
            raise RuntimeError("platform hiccup")
        return StepResult(message="draft created")


def register(ctx, config):
    ctx.steps.register(Prepare())
    ctx.steps.register(Publish())
    ctx.workflows.add_default("release", ["prepare", "publish"])

register.inject = ["steps", "workflows"]

HEARD = []


def listener(ctx, config):
    ctx.on("approval/request", lambda task: HEARD.append(task["id"]))

listener.inject = []


def wave_through(ctx, config):
    ctx.on("step/pre-execute", lambda req, next_: Allow())


def ask_for_prepare(ctx, config):
    def policy(req, next_):
        return Ask("prepare needs a look") if req.step.name == "prepare" else next_()
    ctx.on("step/pre-execute", policy)


def junk(ctx, config):
    ctx.on("step/pre-execute", lambda req, next_: "yes please")


def broken(ctx, config):
    def policy(req, next_):
        raise RuntimeError("policy crashed")
    ctx.on("step/pre-execute", policy)


def guard_all(ctx, config):
    ctx.steps.guard(lambda req: "maintenance window" if req.step.name == "prepare" else None)

guard_all.inject = ["steps"]
"""


@pytest.fixture
def make(tmp_path, monkeypatch):
    (tmp_path / "release_steps.py").write_text(STEPS, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "release_steps", raising=False)

    def build(extra_entries=(), extra_patch=""):
        inserts = [{"id": "release", "plugin": "release_steps:register"}]
        inserts += [{"id": e, "plugin": f"release_steps:{e}"} for e in extra_entries]
        path = tmp_path / "runspool.yaml"
        path.write_text(
            f"workspace_root: {tmp_path / 'ws'}\n"
            f"patch:\n  - insert: {json.dumps(inserts)}\n" + extra_patch,
            encoding="utf-8",
        )
        ctx = load_context(path)
        module = sys.modules["release_steps"]
        module.RUNS.clear()
        module.HEARD.clear()
        module.Publish.fail_times = 0
        return ctx, module, path

    return build


def run(ctx):
    run_until_idle(ctx, notifier=lambda m: None)


def task(ctx, tid=1):
    return ctx.repo.get_task(tid)


def events(ctx, tid=1):
    return [e["event_type"] for e in reversed(ctx.log.list_for_task(tid))]


def test_a_step_with_side_effects_waits_for_approval_then_runs(make):
    ctx, mod, _ = make(["listener"])
    tid = ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    assert task(ctx)["task_status"] == TaskStatus.AWAITING_APPROVAL
    assert mod.RUNS == [("prepare", 1)]  # publish has not run
    assert mod.HEARD == [tid]  # approval/request reached the listener
    ctx.service("tasks").approve(tid, by="ethan")
    run(ctx)
    assert task(ctx)["task_status"] == TaskStatus.COMPLETED
    assert mod.RUNS == [("prepare", 1), ("publish", 1)]
    trail = events(ctx)
    assert trail.index("approval_asked") < trail.index("approval_decided")
    decided = next(e for e in ctx.log.list_for_task(tid) if e["event_type"] == "approval_decided")
    assert json.loads(decided["payload_json"]) == {
        "outcome": "approved",
        "by": "ethan",
        "attempt": 1,
    }


def test_rejecting_needs_attention_and_retry_asks_again(make):
    ctx, mod, _ = make()
    tid = ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    ctx.service("tasks").reject(tid, by="ethan", reason="wrong cover")
    assert task(ctx)["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "wrong cover" in task(ctx)["last_error"]
    ctx.service("tasks").retry(tid)
    run(ctx)
    assert task(ctx)["task_status"] == TaskStatus.AWAITING_APPROVAL
    assert ("publish", 1) not in mod.RUNS


def test_a_grant_covers_one_attempt_only(make):
    ctx, mod, _ = make()
    mod.Publish.fail_times = 1
    tid = ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    ctx.service("tasks").approve(tid, by="ethan")
    run(ctx)  # publish attempt 1 fails; its automatic retry is attempt 2
    assert task(ctx)["task_status"] == TaskStatus.AWAITING_APPROVAL
    assert mod.RUNS.count(("publish", 1)) == 1 and ("publish", 2) not in mod.RUNS


def test_a_crash_after_approval_asks_again(make):
    ctx, mod, _ = make()
    tid = ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    ctx.service("tasks").approve(tid, by="ethan")
    # The approved attempt starts, then the process dies mid-step.
    sm = ctx.state_machine("release")
    assert sm.claim(tid, worker="w", token="t")
    ctx.step_runs.start(tid, "publish")
    sm.recover_interrupted()
    run(ctx)
    assert task(ctx)["task_status"] == TaskStatus.AWAITING_APPROVAL
    assert ("publish", 2) not in mod.RUNS


def test_policy_never_refuses_instead_of_asking(make):
    ctx, mod, _ = make(extra_patch="  - id: approval\n    config: {policy: never}\n")
    ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    assert task(ctx)["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "'never'" in task(ctx)["last_error"]
    assert all(step != "publish" for step, _ in mod.RUNS)


def test_without_an_approval_service_side_effects_are_refused(make):
    ctx, mod, _ = make(extra_patch="  - id: approval\n    disabled: true\n")
    ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    assert task(ctx)["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "no approval service" in task(ctx)["last_error"]
    assert all(step != "publish" for step, _ in mod.RUNS)


def test_a_policy_cannot_wave_a_side_effect_through(make):
    ctx, mod, _ = make(["wave_through"])
    ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    assert task(ctx)["task_status"] == TaskStatus.AWAITING_APPROVAL


def test_a_policy_can_ask_for_any_step_and_one_approval_settles_it(make):
    ctx, mod, _ = make(["ask_for_prepare"])
    tid = ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    assert (task(ctx)["task_status"], task(ctx)["step"]) == (
        TaskStatus.AWAITING_APPROVAL,
        "prepare",
    )
    ctx.service("tasks").approve(tid, by="ethan")
    run(ctx)
    assert (task(ctx)["task_status"], task(ctx)["step"]) == (
        TaskStatus.AWAITING_APPROVAL,
        "publish",
    )
    assert mod.RUNS == [("prepare", 1)]


@pytest.mark.parametrize("entry, message", [("junk", "not a decision"), ("broken", "crashed")])
def test_a_bad_policy_fails_closed(make, entry, message):
    ctx, mod, _ = make([entry])
    ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    assert task(ctx)["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert message in task(ctx)["last_error"]
    assert mod.RUNS == []


def test_a_guard_can_refuse(make):
    ctx, mod, _ = make(["guard_all"])
    ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    assert "maintenance window" in task(ctx)["last_error"]
    assert mod.RUNS == []


def test_terminating_a_waiting_task(make):
    ctx, mod, _ = make()
    tid = ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    ctx.service("tasks").terminate(tid)
    assert task(ctx)["task_status"] == TaskStatus.TERMINATED


def test_a_runner_without_a_gate_refuses_side_effects(make):
    ctx, mod, _ = make()
    registry = StepRegistry()
    registry.register(mod.Publish())
    runner = TaskRunner(
        ctx.repo, ctx.log, ctx.step_runs, registry, ctx.config, notifier=lambda m: None
    )
    tid = ctx.repo.create_task(input="x", workflow="release", first_step="publish", max_retries=1)
    ctx.state_machine("release").claim(tid, worker="w")
    runner.execute(tid)
    assert task(ctx, tid)["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert mod.RUNS == []


def test_a_lazily_loaded_step_keeps_its_side_effect_flag(tmp_path, monkeypatch):
    (tmp_path / "lazy_steps.py").write_text(STEPS, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, "lazy_steps", raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        "steps:\n  publish:\n    import: 'lazy_steps:Publish'\n"
        "workflows:\n  w:\n    steps: [publish]\n",
        encoding="utf-8",
    )
    ctx = load_context(path)
    ctx.service("tasks").add("x", workflow="w")
    run(ctx)
    assert task(ctx)["task_status"] == TaskStatus.AWAITING_APPROVAL


def test_gate_request_knows_its_grant():
    from runspool.engine.step import Step, StepResult

    class S(Step):
        name = "s"

        def run(self, ctx):
            return StepResult()

    granted = GateRequest({"approval_grant": "granted:s:2"}, S(), 2)
    assert granted.granted and not GateRequest({"approval_grant": "granted:s:2"}, S(), 3).granted
    assert isinstance(Allow(), Allow) and Ask("x").reason == "x"


def test_cli_approve_and_reject(make):
    ctx, mod, path = make()
    cli = CliRunner()
    ctx.service("tasks").add("v1.0", workflow="release")
    run(ctx)
    view = json.loads(cli.invoke(app, ["-c", str(path), "inspect", "1", "--json"]).output)
    assert view["available_actions"] == ["approve", "reject", "terminate"]
    assert "side effects" in view["suggested_next_action"]
    assert cli.invoke(app, ["-c", str(path), "approve", "1"]).exit_code == 0
    again = cli.invoke(app, ["-c", str(path), "approve", "1"])
    assert again.exit_code == 1 and "awaiting_approval" in again.output
    assert task(ctx)["approval_grant"].startswith("granted:publish:")
