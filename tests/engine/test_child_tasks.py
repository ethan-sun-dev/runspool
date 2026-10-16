"""Child tasks spawned by a step result: created atomically with the parent's step."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys

import pytest

from runspool import commands
from runspool.app import load_context
from runspool.engine.children import ChildTask
from runspool.engine.step import StepResult
from runspool.models import TaskStatus
from runspool.runtime import run_until_idle

STEPS = """
from runspool.engine.artifacts import Artifact
from runspool.engine.children import ChildTask
from runspool.engine.external import ExternalStep
from runspool.engine.step import Step, StepDeferred, StepResult

# children: what fanout spawns, as ChildTask keyword arguments; hook: called with
# the task while fanout runs (a test's way to act mid-step).
STATE = {
    "children": [],
    "degraded": False,
    "hook": None,
    "fail_gate": False,
    "updates": {},
    "skip_when_done": False,  # fanout's when(): skip once metadata.spawn_done is set
    "artifact": False,  # fanout records a "plan" artifact
}


class Fanout(Step):
    name = "fanout"

    def when(self, task, config):
        return not (STATE["skip_when_done"] and (task["metadata"] or {}).get("spawn_done"))

    def run(self, ctx):
        if STATE["hook"] is not None:
            STATE["hook"](ctx.task)
        children = [ChildTask(**c) for c in STATE["children"]]
        artifacts = [Artifact("plan", "计划/plan.md", "ab" * 32)] if STATE["artifact"] else []
        return StepResult(
            message="planned",
            spawn=children,
            degraded=STATE["degraded"],
            updates=STATE["updates"],
            artifacts=artifacts,
        )


class Gate(Step):
    name = "gate"

    def run(self, ctx):
        if STATE["fail_gate"]:
            raise RuntimeError("gate closed")
        return StepResult()


class Work(Step):
    name = "work"

    def run(self, ctx):
        return StepResult(message="worked")


class Slow(Step):
    name = "slow"

    def run(self, ctx):
        raise StepDeferred("waiting on purpose", delay_seconds=3600)


class Approve(ExternalStep):
    name = "approve_split"

    def validate(self, ctx, submission):
        units = submission.value.get("units", [])
        return StepResult(spawn=[ChildTask(u, "unit", key=u) for u in units])
"""

CHILDREN = [
    {
        "input": "/作品/稿件 一.md",
        "workflow": "unit",
        "name": "公众号",
        "metadata": {"平台": "公众号"},
        "key": "wechat",
    },
    {"input": "/作品/稿件 一.md", "workflow": "slow_unit", "metadata": {"平台": "小红书"}},
]


@pytest.fixture
def ws(tmp_path, monkeypatch, request):
    module = "child_steps_" + re.sub(r"\W", "_", request.node.name)
    (tmp_path / f"{module}.py").write_text(STEPS, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delitem(sys.modules, module, raising=False)
    path = tmp_path / "配置 目录" / "runspool.yaml"
    path.parent.mkdir()
    steps = "".join(
        f'  {n}:\n    import: "{module}:{c}"\n'
        for n, c in (
            ("fanout", "Fanout"),
            ("gate", "Gate"),
            ("work", "Work"),
            ("slow", "Slow"),
            ("approve_split", "Approve"),
        )
    )
    path.write_text(
        f"workspace_root: {tmp_path / '工作 区'}\n"
        "scheduler:\n  max_retries: 0\n"
        f"steps:\n{steps}"
        "workflows:\n"
        "  parent:\n    steps: [fanout, gate]\n"
        "  unit:\n    steps: [work]\n"
        "  slow_unit:\n    steps: [slow]\n"
        "  split:\n    steps: [approve_split]\n"
        # Names YAML decodes to a lone surrogate, which SQLite cannot store as UTF-8.
        '  "odd\\uD800":\n    steps: [work]\n'
        '  odd_first:\n    steps: ["w\\uD800"]\n',
        encoding="utf-8",
    )
    ctx = load_context(path)
    state = __import__(module).STATE
    state["children"] = [dict(c) for c in CHILDREN]
    return ctx, path, module, state


def _parent(ctx):
    return ctx.repo.create_task(
        input="/作品/稿件 一.md", workflow="parent", first_step="fanout", max_retries=0
    )


def _spawns(ctx, parent):
    with sqlite3.connect(ctx.db.path) as conn:
        return conn.execute(
            "select child_task_id, step, step_run_id, spawn_key from task_spawns "
            "where parent_task_id = ? order by id",
            (parent,),
        ).fetchall()


def _events(ctx, tid, kind):
    return [e for e in ctx.log.list_for_task(tid, limit=200) if e["event_type"] == kind]


def test_a_step_spawns_independent_children_with_its_transition(ws):
    ctx, _, _, _ = ws
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)

    parent = ctx.repo.get_task(pid)
    assert parent["task_status"] == TaskStatus.COMPLETED, parent["last_error"]  # no waiting
    first, second = ctx.repo.list_children(pid)
    assert (first["workflow"], first["name"], first["metadata"]) == (
        "unit",
        "公众号",
        {"平台": "公众号"},
    )
    assert first["input"] == "/作品/稿件 一.md" and first["task_status"] == TaskStatus.COMPLETED
    assert second["workflow"] == "slow_unit" and second["name"] is None
    assert second["task_status"] == TaskStatus.QUEUED and second["next_retry_at"]  # deferred
    assert second["max_retries"] == 0  # the configured default
    fanout_run = next(r for r in ctx.step_runs.list_for_task(pid) if r["step"] == "fanout")
    assert _spawns(ctx, pid) == [
        (first["id"], "fanout", fanout_run["id"], "wechat"),
        (second["id"], "fanout", fanout_run["id"], None),
    ]
    (spawned,) = _events(ctx, pid, "children_spawned")
    assert json.loads(spawned["payload_json"]) == {
        "created": [first["id"], second["id"]],
        "existing": [],
    }
    created = _events(ctx, first["id"], "created")
    assert created[0]["message"] == f"task created from {pid}"


def test_a_crash_inside_the_transaction_leaves_neither_and_a_rerun_spawns_once(ws):
    """The process dies after the children rows are written but before the parent's
    transition commits: nothing of it is kept, and the step runs again."""
    ctx, path, _, _ = ws
    pid = _parent(ctx)
    _run_crashing(path, "inside")
    after = load_context(path)
    assert after.repo.list_children(pid) == []
    assert _spawns(after, pid) == []
    task = after.repo.get_task(pid)
    assert (task["task_status"], task["step"]) == (TaskStatus.RUNNING, "fanout")  # not advanced

    run_until_idle(after, notifier=lambda m: None)  # recovers the interrupted step, reruns it
    assert len(after.repo.list_children(pid)) == 2
    assert len(_spawns(after, pid)) == 2
    assert after.repo.get_task(pid)["task_status"] == TaskStatus.COMPLETED


def test_a_crash_after_the_transaction_keeps_both_and_a_rerun_adds_nothing(ws):
    ctx, path, _, _ = ws
    pid = _parent(ctx)
    _run_crashing(path, "after")
    after = load_context(path)
    task = after.repo.get_task(pid)
    assert (task["task_status"], task["step"]) == (TaskStatus.QUEUED, "gate")  # advanced
    assert len(after.repo.list_children(pid)) == 2
    run_until_idle(after, notifier=lambda m: None)
    assert len(after.repo.list_children(pid)) == 2
    assert len(_spawns(after, pid)) == 2
    assert after.repo.get_task(pid)["task_status"] == TaskStatus.COMPLETED


_CRASH = """
import os
import sys

mode, profile, module = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, os.path.dirname(os.path.dirname(profile)))
state = __import__(module).STATE
state["children"] = {children}
state["updates"] = {"metadata": {"spawn_done": True}}
state["skip_when_done"] = True

from runspool.app import load_context
from runspool.persistence.repository import TaskRepository
from runspool.persistence.state_machine import StateMachine
from runspool.runtime import run_until_idle

real_spawner = TaskRepository.spawner
real_finish = StateMachine.finish_step


def spawner(self, parent, plan):
    hook = real_spawner(self, parent, plan)

    def crashing(conn):
        hook(conn)  # the children are written in this transaction...
        os._exit(3)  # ...and the process dies before it commits

    return crashing


def finish_step(self, task_id, outcome, **kw):
    after = real_finish(self, task_id, outcome, **kw)
    if getattr(outcome, "spawn", None) is not None:
        os._exit(3)  # dies right after the transaction committed
    return after


if mode == "inside":
    TaskRepository.spawner = spawner
else:
    StateMachine.finish_step = finish_step
run_until_idle(load_context(profile), notifier=lambda m: None)
"""


def _run_crashing(path, mode):
    module = next(p.stem for p in path.parent.parent.glob("child_steps_*.py"))
    script = _CRASH.replace("{children}", repr([dict(c) for c in CHILDREN]))
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    result = subprocess.run(
        [sys.executable, "-c", script, mode, str(path), module],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert result.returncode == 3, result.stdout + result.stderr


def test_a_stale_worker_spawns_nothing(ws):
    """A worker whose claim was taken back meanwhile changes nothing at its step
    boundary, children included; the step's next run spawns them once."""
    ctx, _, _, state = ws
    pid = _parent(ctx)

    def reclaim(task):
        if state["hook"] is not None:
            state["hook"] = None
            ctx.state_machine("parent").requeue_stale(task["id"])

    state["hook"] = reclaim
    from runspool.engine.runner import TaskRunner

    sm = ctx.state_machine("parent")
    assert sm.claim(pid, worker="w", token="first")
    TaskRunner(
        ctx.repo,
        ctx.log,
        ctx.step_runs,
        ctx.service("steps").registry,
        ctx.config,
        notifier=lambda m: None,
        items=ctx.items,
    ).execute(pid, claim_token="first")
    assert ctx.repo.list_children(pid) == []
    assert ctx.repo.get_task(pid)["task_status"] == TaskStatus.QUEUED  # requeued, same step
    run_until_idle(ctx, notifier=lambda m: None)
    assert len(ctx.repo.list_children(pid)) == 2


def test_a_rerun_after_set_step_does_not_spawn_a_keyed_child_again(ws):
    ctx, _, _, state = ws
    state["fail_gate"] = True
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    assert ctx.repo.get_task(pid)["task_status"] == TaskStatus.MANUAL_REQUIRED
    keyed = ctx.repo.list_children(pid)[0]["id"]

    commands.set_step(ctx, pid, "fanout")  # run the spawning step again
    commands.retry_task(ctx, pid)
    state["fail_gate"] = False
    run_until_idle(ctx, notifier=lambda m: None)
    children = ctx.repo.list_children(pid)
    assert len(children) == 3  # the keyed child once, the unkeyed one twice
    assert [c["workflow"] for c in children].count("unit") == 1
    second = _events(ctx, pid, "children_spawned")[0]  # newest first
    payload = json.loads(second["payload_json"])
    assert payload["existing"] == [keyed] and len(payload["created"]) == 1
    assert "1 already spawned" in second["message"]


@pytest.mark.parametrize(
    "children, reason",
    [
        ([{"input": "/私密/稿件.md", "workflow": "nope"}], "child 1: workflow 'nope'"),
        (
            [
                {"input": "/私密/a.md", "workflow": "unit", "key": "k"},
                {"input": "/私密/b.md", "workflow": "unit", "key": "k"},
            ],
            "child 2: key 'k' is used twice",
        ),
        ([{"input": "", "workflow": "unit"}], "child 1: input must be a non-empty string"),
        (
            [{"input": "/私密/稿件.md", "workflow": "unit", "metadata": {"x": object()}}],
            "child 1: metadata must be a JSON object",
        ),
        ([{"input": "/私密/稿件.md", "workflow": "unit", "key": ""}], "key must be a non-empty"),
    ],
)
def test_a_spawn_that_does_not_validate_fails_the_step_and_creates_nothing(ws, children, reason):
    ctx, _, _, state = ws
    state["children"] = children
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(pid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert reason in task["last_error"] and "私密" not in task["last_error"]
    assert ctx.repo.list_children(pid) == [] and _spawns(ctx, pid) == []
    assert task["step"] == "fanout"


def test_a_result_that_is_not_a_child_task_fails_the_step(ws, monkeypatch):
    ctx, _, module, _ = ws
    monkeypatch.setattr(sys.modules[module].Fanout, "run", lambda self, c: StepResult(spawn=["x"]))
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    assert "child 1: not a ChildTask" in ctx.repo.get_task(pid)["last_error"]


def test_terminating_at_the_boundary_spawns_nothing(ws):
    ctx, _, _, state = ws
    state["hook"] = lambda task: commands.terminate_task(ctx, task["id"])
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    assert ctx.repo.get_task(pid)["task_status"] == TaskStatus.TERMINATED
    assert ctx.repo.list_children(pid) == [] and _events(ctx, pid, "children_spawned") == []


def test_pausing_at_the_boundary_still_spawns(ws):
    ctx, _, _, state = ws
    state["hook"] = lambda task: commands.pause_task(ctx, task["id"])
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(pid)
    assert (task["task_status"], task["step"]) == (TaskStatus.PAUSED, "gate")
    assert len(ctx.repo.list_children(pid)) == 2


def test_a_degraded_step_spawns_too(ws):
    ctx, _, _, state = ws
    state["degraded"] = True
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    assert ctx.repo.get_task(pid)["task_status"] == TaskStatus.PARTIALLY_COMPLETED
    assert len(ctx.repo.list_children(pid)) == 2


def test_an_accepted_external_submission_spawns_children(ws):
    ctx, _, _, _ = ws
    pid = ctx.repo.create_task(
        input="拆分 稿件", workflow="split", first_step="approve_split", max_retries=0
    )
    run_until_idle(ctx, notifier=lambda m: None)
    item = ctx.items.active_for(pid)
    token = commands.claim_item(ctx, item["id"], by="人工")
    value = {"units": ["公众号", "知乎"]}
    commands.submit_item(ctx, item["id"], token=token, value=value, by="人工")
    run_until_idle(ctx, notifier=lambda m: None)
    assert ctx.repo.get_task(pid)["task_status"] == TaskStatus.COMPLETED
    assert ctx.items.get(item["id"])["status"] == "accepted"
    assert [c["input"] for c in ctx.repo.list_children(pid)] == ["公众号", "知乎"]


def test_one_parent_never_holds_two_children_under_one_key(ws):
    ctx, _, _, _ = ws
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    child = ctx.repo.list_children(pid)[0]["id"]
    with sqlite3.connect(ctx.db.path) as conn, pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "insert into task_spawns (parent_task_id, child_task_id, step, spawn_key) "
            "values (?, ?, 'fanout', 'wechat')",
            (pid, child),
        )


def test_a_store_that_cannot_spawn_fails_the_step(ws):
    from runspool.engine.runner import TaskRunner

    ctx, _, _, _ = ws

    class NoSpawn:
        def __init__(self, repo):
            self._repo = repo

        def __getattr__(self, name):
            if name == "spawner":
                raise AttributeError(name)
            return getattr(self._repo, name)

    pid = _parent(ctx)
    ctx.state_machine("parent").claim(pid, worker="w")
    TaskRunner(
        NoSpawn(ctx.repo),
        ctx.log,
        ctx.step_runs,
        ctx.service("steps").registry,
        ctx.config,
        notifier=lambda m: None,
    ).execute(pid)
    task = ctx.repo.get_task(pid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "cannot spawn child tasks" in task["last_error"]
    assert ctx.repo.list_children(pid) == []


def test_without_spawning_nothing_is_recorded(tmp_path):
    from typer.testing import CliRunner

    from runspool.cli import app

    cfg = tmp_path / "config.yaml"
    runner = CliRunner()
    assert runner.invoke(app, ["-c", str(cfg), "init", "--workspace-root", str(tmp_path / "ws")])
    src = tmp_path / "笔记.txt"
    src.write_text("x", encoding="utf-8")
    for args in (["add", str(src)], ["run"]):
        assert runner.invoke(app, ["-c", str(cfg), *args]).exit_code == 0
    ctx = load_context(cfg)
    assert _spawns(ctx, 1) == [] and _events(ctx, 1, "children_spawned") == []
    assert ctx.repo.list_children(1) == []


def test_child_task_is_a_plain_value():
    child = ChildTask("in", "unit")
    assert (child.name, child.metadata, child.key) == (None, None, None)
    assert StepResult().spawn == ()


# -- declarations that cannot be written fail the step before anything is written ---


def _assert_failed_cleanly(ctx, pid, reason):
    task = ctx.repo.get_task(pid)
    assert (task["task_status"], task["step"]) == (TaskStatus.MANUAL_REQUIRED, "fanout")
    assert reason in task["last_error"]
    (run,) = [r for r in ctx.step_runs.list_for_task(pid) if r["step"] == "fanout"]
    assert run["status"] == "failed" and reason in run["error"]
    assert task["metadata"] == {}  # the result's updates were not written
    assert ctx.repo.list_children(pid) == [] and _spawns(ctx, pid) == []


def test_metadata_that_cannot_be_stored_fails_the_step_before_anything_is_written(ws):
    """{"a": 1, 2: 3} serializes, but not with sorted keys as it is stored."""
    ctx, _, _, state = ws
    state["children"] = [{"input": "/私密/稿件.md", "workflow": "unit", "metadata": {"a": 1, 2: 3}}]
    state["updates"] = {"metadata": {"seen": True}}
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    _assert_failed_cleanly(ctx, pid, "child 1: metadata must be a JSON object")


@pytest.mark.parametrize("spawn", [{}, 0, False, ""], ids=["dict", "zero", "false", "str"])
def test_a_spawn_of_the_wrong_type_fails_the_step_even_when_empty(ws, monkeypatch, spawn):
    ctx, _, module, _ = ws
    monkeypatch.setattr(
        sys.modules[module].Fanout,
        "run",
        lambda self, c: StepResult(spawn=spawn, updates={"metadata": {"seen": True}}),
    )
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    _assert_failed_cleanly(ctx, pid, "spawn must be a list of ChildTask")


@pytest.mark.parametrize("spawn", [(), []], ids=["tuple", "list"])
def test_an_empty_list_spawns_nothing(ws, monkeypatch, spawn):
    ctx, _, module, _ = ws
    monkeypatch.setattr(sys.modules[module].Fanout, "run", lambda self, c: StepResult(spawn=spawn))
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    assert ctx.repo.get_task(pid)["task_status"] == TaskStatus.COMPLETED
    assert ctx.repo.list_children(pid) == [] and _events(ctx, pid, "children_spawned") == []


def test_a_store_error_while_creating_children_fails_the_step_not_leaves_it_running(
    ws, monkeypatch
):
    from runspool.persistence.repository import TaskRepository

    ctx, _, _, _ = ws
    real = TaskRepository.spawner

    def broken(self, parent, plan):
        hook = real(self, parent, plan)

        def failing(conn):
            hook(conn)
            raise sqlite3.IntegrityError("simulated")

        return failing

    monkeypatch.setattr(TaskRepository, "spawner", broken)
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(pid)
    assert (task["task_status"], task["step"]) == (TaskStatus.MANUAL_REQUIRED, "fanout")
    assert task["last_error"] == "cannot create the child tasks: IntegrityError"
    (run,) = [r for r in ctx.step_runs.list_for_task(pid) if r["step"] == "fanout"]
    assert run["status"] == "failed"
    assert ctx.repo.list_children(pid) == [] and _spawns(ctx, pid) == []


# -- a spawn's result is written with its transition, all or nothing -----------------


def _break_spawner(monkeypatch):
    from runspool.persistence.repository import TaskRepository

    real = TaskRepository.spawner

    def broken(self, parent, plan):
        hook = real(self, parent, plan)

        def failing(conn):
            hook(conn)
            raise sqlite3.IntegrityError("simulated")

        return failing

    monkeypatch.setattr(TaskRepository, "spawner", broken)


def test_a_failed_spawn_keeps_no_update_so_a_retry_still_spawns(ws, monkeypatch):
    """A marker the step's when() reads must not survive a spawn that failed:
    otherwise the retry skips the step and the children are never created."""
    ctx, _, _, state = ws
    state["updates"] = {"metadata": {"spawn_done": True}}
    state["skip_when_done"] = True
    _break_spawner(monkeypatch)
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(pid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert task["metadata"] == {}  # the update went with the failed transaction

    monkeypatch.undo()
    commands.retry_task(ctx, pid)
    run_until_idle(ctx, notifier=lambda m: None)
    task = ctx.repo.get_task(pid)
    assert task["task_status"] == TaskStatus.COMPLETED
    assert task["metadata"] == {"spawn_done": True}
    assert len(ctx.repo.list_children(pid)) == 2


def test_a_crash_inside_the_transaction_keeps_no_update_either(ws):
    ctx, path, _, _ = ws
    pid = _parent(ctx)
    _run_crashing(path, "inside")  # the script sets the spawn_done update and when()
    after = load_context(path)
    task = after.repo.get_task(pid)
    assert task["metadata"] == {}
    (run,) = after.step_runs.list_for_task(pid)
    assert run["status"] == "running"  # not "ok": the run is closed with the transition
    state = __import__(next(p.stem for p in path.parent.parent.glob("child_steps_*.py"))).STATE
    state["updates"] = {"metadata": {"spawn_done": True}}
    state["skip_when_done"] = True
    run_until_idle(after, notifier=lambda m: None)
    assert len(after.repo.list_children(pid)) == 2
    assert after.repo.get_task(pid)["metadata"] == {"spawn_done": True}
    statuses = [r["status"] for r in after.step_runs.list_for_task(pid) if r["step"] == "fanout"]
    assert statuses == ["interrupted", "ok"]


def test_a_failed_read_after_the_commit_does_not_rewrite_the_committed_run(ws, monkeypatch):
    from runspool.persistence.repository import TaskRepository

    ctx, _, _, state = ws
    state["artifact"] = True
    real_transition, real_get = TaskRepository.transition, TaskRepository.get_task
    flags = {"armed": True, "committed": False}

    def transition(self, *args, **kwargs):
        applied = real_transition(self, *args, **kwargs)
        if applied and kwargs.get("also") is not None and flags["armed"]:
            flags["armed"] = False
            flags["committed"] = True  # the spawning transition has just committed
        return applied

    def get_task(self, task_id):
        if flags["committed"]:
            flags["committed"] = False
            raise sqlite3.OperationalError("simulated read failure")
        return real_get(self, task_id)

    monkeypatch.setattr(TaskRepository, "transition", transition)
    monkeypatch.setattr(TaskRepository, "get_task", get_task)
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    monkeypatch.undo()
    (run,) = [r for r in ctx.step_runs.list_for_task(pid) if r["step"] == "fanout"]
    assert run["status"] == "ok" and run["error"] is None
    assert len(ctx.repo.list_children(pid)) == 2
    (artifact,) = ctx.service("store").artifacts.latest(pid)
    assert artifact["type"] == "plan" and artifact["step_run_id"] == run["id"]
    assert ctx.repo.get_task(pid)["task_status"] == TaskStatus.COMPLETED


def test_metadata_that_utf8_cannot_encode_is_refused_before_anything_is_written(ws):
    ctx, _, _, state = ws
    state["children"] = [
        {"input": "/私密/稿件.md", "workflow": "unit", "metadata": {"x": "\ud800"}}
    ]
    state["updates"] = {"metadata": {"seen": True}}
    state["artifact"] = True
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    _assert_failed_cleanly(ctx, pid, "child 1: metadata must be a JSON object")
    store = ctx.service("store")
    assert store.artifacts.list_for_task(pid, include_unfinished=True) == []


@pytest.mark.parametrize("field", ["input", "name", "key"])
def test_text_that_utf8_cannot_encode_is_refused_before_anything_is_written(ws, field):
    ctx, _, _, state = ws
    child = {"input": "/私密/稿件.md", "workflow": "unit", field: "坏\ud800"}
    state["children"] = [child]
    state["updates"] = {"metadata": {"seen": True}}
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    _assert_failed_cleanly(ctx, pid, f"child 1: {field}")


@pytest.mark.parametrize(
    "workflow, field",
    [("odd\ud800", "workflow"), ("odd_first", "first step")],
    ids=["workflow", "first_step"],
)
def test_a_workflow_or_first_step_name_utf8_cannot_encode_is_refused_first(ws, workflow, field):
    ctx, _, _, state = ws
    assert workflow in ctx.config.workflows  # defined, so only its text is wrong
    state["children"] = [{"input": "/私密/稿件.md", "workflow": workflow}]
    state["updates"] = {"metadata": {"seen": True}}
    state["artifact"] = True
    pid = _parent(ctx)
    run_until_idle(ctx, notifier=lambda m: None)
    store = ctx.service("store")
    assert store.artifacts.list_for_task(pid, include_unfinished=True) == []  # not recorded
    assert store.artifacts.latest(pid) == []  # so not visible by default either
    _assert_failed_cleanly(ctx, pid, f"child 1: {field} must be valid UTF-8 text")
    assert "\ud800" not in ctx.repo.get_task(pid)["last_error"]
