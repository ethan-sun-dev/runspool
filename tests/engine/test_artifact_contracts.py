"""Artifact contracts: declarations, the chain check, doctor, startup and recording."""

from __future__ import annotations

import hashlib
import json
import os
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from runspool.app import load_context
from runspool.cli import app
from runspool.engine.artifacts import (
    Artifact,
    ArtifactType,
    ArtifactTypeError,
    Contract,
    check_chain,
    contract_of,
    parse_type,
)
from runspool.engine.registry import StepRegistry
from runspool.engine.runner import TaskRunner
from runspool.engine.step import Step, StepResult
from runspool.models import TaskStatus
from runspool.persistence.state_machine import StateMachine
from runspool.runtime import run_until_idle
from tests.support import force_fields

# -- types ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text, kind, param",
    [
        ("transcript", "transcript", None),
        ("variant:blog", "variant", "blog"),
        ("variant:<target>", "variant", "<target>"),
        ("video:9x16.v2", "video", "9x16.v2"),
        ("source.text", "source.text", None),
    ],
)
def test_types_parse(text, kind, param):
    t = parse_type(text)
    assert (t.kind, t.param) == (kind, param)
    assert str(t) == text


@pytest.mark.parametrize(
    "text",
    ["", "Variant", "1st", "variant:", "variant:<Target>", "variant:<>", "a:b:c", "a b", "变体"],
)
def test_malformed_types_are_refused(text):
    with pytest.raises(ArtifactTypeError, match="invalid artifact type"):
        parse_type(text)


@pytest.mark.parametrize(
    "provided, required, ok",
    [
        ("transcript", "transcript", True),
        ("transcript", "subtitles", False),
        ("variant:blog", "variant", False),  # a plain kind is not a parameterised one
        ("variant", "variant:<target>", False),
        ("variant:blog", "variant:<target>", True),
        ("variant:<any>", "variant:<target>", True),
        ("variant:<target>", "variant:blog", True),  # a placeholder provides any value
        ("variant:blog", "variant:blog", True),
        ("variant:blog", "variant:news", False),
    ],
)
def test_matching(provided, required, ok):
    assert parse_type(provided).satisfies(parse_type(required)) is ok


# -- declarations --------------------------------------------------------------------


class _Plain(Step):
    name = "plain"

    def run(self, ctx):
        return StepResult()


class _Declared(Step):
    name = "declared"
    requires = ("source:file",)
    provides = ["variant:<target>"]

    def run(self, ctx):
        return StepResult()


def test_a_step_declares_nothing_by_default():
    assert contract_of(_Plain()) == Contract()


def test_declarations_are_parsed():
    c = contract_of(_Declared())
    assert c.declared
    assert c.requires == (ArtifactType("source", "file"),)
    assert c.provides == (ArtifactType("variant", "<target>"),)


def test_a_declaration_must_be_a_list():
    class Bad(_Plain):
        requires = "transcript"

    with pytest.raises(ArtifactTypeError, match="must be a list"):
        contract_of(Bad())


# -- the chain check -----------------------------------------------------------------


def _contracts(**declared):
    def lookup(name):
        if name not in declared:
            return Contract()
        requires, provides = declared[name]
        return Contract(
            tuple(parse_type(t) for t in requires), tuple(parse_type(t) for t in provides), True
        )

    return lookup


def test_a_satisfied_chain_has_no_findings():
    report = check_chain(
        "wf",
        ["fetch", "write", "adapt"],
        _contracts(
            fetch=((), ("source:file",)),
            write=(("source:file",), ("master",)),
            adapt=(("master",), ("variant:<target>",)),
        ),
    )
    assert report.declared and report.ok and not report.unverified


def test_a_missing_requirement_names_the_step_the_type_and_what_is_available():
    report = check_chain(
        "wf",
        ["fetch", "adapt"],
        _contracts(fetch=((), ("source:file",)), adapt=(("master",), ())),
    )
    (error,) = report.errors
    assert "workflow 'wf'" in error and "step 'adapt' requires 'master'" in error
    assert "'source:file' (step 'fetch')" in error


def test_a_requirement_must_come_from_an_earlier_step():
    report = check_chain(
        "wf",
        ["adapt", "write"],
        _contracts(adapt=(("master",), ()), write=((), ("master",))),
    )
    assert len(report.errors) == 1
    assert "provided before it: nothing" in report.errors[0]


def test_a_gap_after_an_undeclared_step_is_an_error_by_default():
    report = check_chain("wf", ["legacy", "adapt"], _contracts(adapt=(("master",), ())))
    (error,) = report.errors
    assert "'master'" in error and "'legacy'" in error and "allow_unverified" in error


def test_allow_unverified_turns_that_gap_into_a_note():
    report = check_chain(
        "wf", ["legacy", "adapt"], _contracts(adapt=(("master",), ())), allow_unverified=True
    )
    assert report.ok
    (note,) = report.unverified
    assert "'legacy'" in note and "'master'" in note


def test_a_fully_undeclared_chain_is_left_alone():
    report = check_chain("wf", ["legacy", "other"], _contracts())
    assert not report.declared and report.ok and not report.unverified


def test_workflow_inputs_provide_types_before_the_first_step():
    lookup = _contracts(write=(("source:file",), ("master",)))
    assert check_chain("wf", ["write"], lookup, inputs=["source:file"]).ok
    assert not check_chain("wf", ["write"], lookup).ok


def test_a_placeholder_provider_satisfies_a_concrete_requirement_and_not_the_reverse():
    ok = check_chain(
        "wf",
        ["adapt", "publish"],
        _contracts(adapt=((), ("variant:<target>",)), publish=(("variant:blog",), ())),
    )
    assert ok.ok
    bad = check_chain(
        "wf",
        ["adapt", "publish"],
        _contracts(adapt=((), ("variant:news",)), publish=(("variant:blog",), ())),
    )
    assert not bad.ok


def test_nothing_declared_means_nothing_to_report():
    report = check_chain("wf", ["a", "b"], _contracts())
    assert not report.declared and report.ok and not report.unverified


def test_a_malformed_declaration_is_an_error_of_the_workflow():
    def lookup(name):
        raise ArtifactTypeError("step 'x': requires: invalid artifact type 'X'")

    report = check_chain("wf", ["x"], lookup)
    assert report.declared and "workflow 'wf'" in report.errors[0]


# -- through a real profile ----------------------------------------------------------

STEPS_MODULE = """
from pathlib import Path

from runspool.engine.artifacts import Artifact
from runspool.engine.step import Step, StepResult


class Fetch(Step):
    name = "fetch"
    provides = ("source:file",)

    def run(self, ctx):
        return StepResult()


class Write(Step):
    name = "write"
    requires = ("source:file",)
    provides = ("master",)

    def run(self, ctx):
        out = Path(ctx.config.workspace_root) / "稿件 目录" / f"母稿-{ctx.task['id']}.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("正文", encoding="utf-8")
        relative = out.relative_to(ctx.config.workspace_root)
        return StepResult(artifacts=[Artifact("master", relative)])


class Adapt(Step):
    name = "adapt"
    requires = ("master",)
    provides = ("variant:<target>",)

    def run(self, ctx):
        return StepResult(artifacts=[Artifact("variant:blog", "elsewhere/blog.md", "ab" * 32)])


class NeedsMissing(Step):
    name = "needs_missing"
    requires = ("transcript",)

    def run(self, ctx):
        return StepResult()


class WrongType(Step):
    name = "wrong_type"
    provides = ("master",)

    def run(self, ctx):
        return StepResult(artifacts=[Artifact("transcript", "x", "ab" * 32)])


class NoFile(Step):
    name = "no_file"
    provides = ("master",)

    def run(self, ctx):
        return StepResult(artifacts=[Artifact("master", "私密/不存在.md")])


class Unreadable(Step):
    name = "unreadable"
    provides = ("master",)

    def run(self, ctx):
        out = Path(ctx.config.workspace_root) / "私密 目录" / "母稿.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("x", encoding="utf-8")
        out.chmod(0)
        return StepResult(artifacts=[Artifact("master", out)])


class Unstatable(Step):
    name = "unstatable"
    provides = ("master",)

    def run(self, ctx):
        out = Path(ctx.config.workspace_root) / "私密 目录" / "母稿.md"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("x", encoding="utf-8")
        return StepResult(artifacts=[Artifact("master", out, "ab" * 32)])


class BadDeclaration(Step):
    name = "bad_declaration"
    requires = 123

    def run(self, ctx):
        return StepResult()


class Undeclared(Step):
    name = "undeclared"

    def run(self, ctx):
        return StepResult(artifacts=[Artifact("anything:goes", "y", "cd" * 32)])


class RecordsThenFails(Step):
    name = "records_then_fails"

    def run(self, ctx):
        return StepResult(
            artifacts=[Artifact("master", "z", "ef" * 32)], updates={"not_a_column": 1}
        )
"""

_CLASSES = {
    "fetch": "Fetch",
    "write": "Write",
    "adapt": "Adapt",
    "needs_missing": "NeedsMissing",
    "wrong_type": "WrongType",
    "no_file": "NoFile",
    "undeclared": "Undeclared",
    "bad_declaration": "BadDeclaration",
    "unreadable": "Unreadable",
    "unstatable": "Unstatable",
    "records_then_fails": "RecordsThenFails",
}


@pytest.fixture
def profile(tmp_path, monkeypatch, request):
    """``profile(workflows)`` writes a profile whose steps come from STEPS_MODULE."""
    module = f"contract_steps_{request.node.name.replace('[', '_').replace(']', '_')}"
    (tmp_path / f"{module}.py").write_text(STEPS_MODULE, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))

    def make(workflows: str):
        steps = "".join(
            f'  {name}:\n    import: "{module}:{cls}"\n' for name, cls in _CLASSES.items()
        )
        path = tmp_path / "配置 目录" / "runspool.yaml"
        path.parent.mkdir(exist_ok=True)
        path.write_text(
            f"workspace_root: {tmp_path / '工作 区'}\n"
            f"steps:\n{steps}"
            f"workflows:\n{textwrap.indent(textwrap.dedent(workflows), '  ')}",
            encoding="utf-8",
        )
        return path

    return make


def _doctor(ctx, name="artifacts"):
    return next((c for c in ctx.service("doctor").run() if c.name == name), None)


def test_a_broken_chain_fails_doctor(profile):
    path = profile(
        """
        drafts:
          steps: [fetch, needs_missing]
        """
    )
    ctx = load_context(path)
    check = _doctor(ctx)
    assert check is not None and not check.ok
    assert "'needs_missing' requires 'transcript'" in check.detail

    result = CliRunner().invoke(app, ["-c", str(path), "doctor", "--json"])
    assert result.exit_code != 0
    by_name = {c["name"]: c for c in json.loads(result.output)}
    assert by_name["artifacts"]["ok"] is False


def test_doctor_reports_a_satisfied_chain(profile):
    ctx = load_context(profile("drafts:\n  steps: [fetch, write, adapt]\n"))
    check = _doctor(ctx)
    assert check.ok and check.detail == "every declared requirement is provided"


def test_doctor_fails_a_gap_after_an_undeclared_step_unless_allowed(profile):
    ctx = load_context(profile("drafts:\n  steps: [undeclared, write]\n"))
    check = _doctor(ctx)
    assert not check.ok and "'write' requires 'source:file'" in check.detail

    ctx = load_context(profile("drafts:\n  allow_unverified: true\n  steps: [undeclared, write]\n"))
    check = _doctor(ctx)
    assert check.ok and check.detail.startswith("unverified:")
    assert "'write' requires 'source:file'" in check.detail


def test_workflow_inputs_satisfy_the_first_step(profile):
    ctx = load_context(profile("drafts:\n  inputs: [source:file]\n  steps: [write]\n"))
    assert _doctor(ctx).ok


def test_doctor_without_any_declaration_has_no_artifacts_check(profile):
    ctx = load_context(profile("drafts:\n  steps: [undeclared]\n"))
    assert _doctor(ctx) is None


def _run(path, first_step="fetch"):
    ctx = load_context(path)
    tid = ctx.repo.create_task(
        input="in.txt", workflow="drafts", first_step=first_step, max_retries=0
    )
    run_until_idle(ctx)
    return ctx, tid


def test_recorded_artifacts_are_hashed_and_listed(profile):
    ctx, tid = _run(profile("drafts:\n  steps: [fetch, write, adapt]\n"))
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.COMPLETED
    rows = ctx.service("store").artifacts.list_for_task(tid)
    assert [(r["type"], r["step"]) for r in rows] == [
        ("master", "write"),
        ("variant:blog", "adapt"),
    ]
    master = rows[0]
    file = ctx.config.workspace_root / master["path"]
    assert master["path"] == f"稿件 目录/母稿-{tid}.md"  # stored as given, Chinese intact
    assert master["sha256"] == hashlib.sha256(file.read_bytes()).hexdigest()
    assert master["size_bytes"] == len("正文".encode())
    assert rows[1]["sha256"] == "ab" * 32 and rows[1]["size_bytes"] is None

    result = CliRunner().invoke(
        app, ["-c", str(ctx.config.base_dir / "runspool.yaml"), "artifacts", str(tid), "--json"]
    )
    assert result.exit_code == 0, result.output
    view = json.loads(result.output)
    assert [a["type"] for a in view] == ["master", "variant:blog"]
    assert view[0]["path"] == f"稿件 目录/母稿-{tid}.md"
    assert set(view[0]) == {
        "id",
        "type",
        "path",
        "sha256",
        "size_bytes",
        "step",
        "step_run_id",
        "created_at",
    }


def test_a_rerun_keeps_history_and_latest_shows_the_newest(profile):
    ctx, tid = _run(profile("drafts:\n  steps: [fetch, write]\n"))
    force_fields(ctx.repo, tid, {"task_status": TaskStatus.QUEUED, "step": "write"})
    run_until_idle(ctx)
    log = ctx.service("store").artifacts
    assert len(log.list_for_task(tid)) == 2
    (latest,) = log.latest(tid)
    assert latest["id"] == log.list_for_task(tid)[-1]["id"]


@pytest.mark.parametrize(
    "step, reason",
    [
        ("wrong_type", "is not a type the step provides (it declares 'master')"),
        ("no_file", "no file at its path to hash"),
    ],
)
def test_an_invalid_artifact_fails_the_step_and_records_nothing(profile, step, reason):
    ctx, tid = _run(profile(f"drafts:\n  steps: [{step}]\n"), first_step=step)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] in (TaskStatus.FAILED, TaskStatus.MANUAL_REQUIRED)
    assert reason in task["last_error"]
    assert "私密" not in task["last_error"]  # paths stay out of errors (and logs)
    log = ctx.service("store").artifacts
    assert log.list_for_task(tid, include_unfinished=True) == []


def test_a_step_that_declares_nothing_may_record_any_concrete_type(profile):
    ctx, tid = _run(profile("drafts:\n  steps: [undeclared]\n"), first_step="undeclared")
    (row,) = ctx.service("store").artifacts.list_for_task(tid)
    assert row["type"] == "anything:goes"


def test_rows_from_a_run_that_failed_afterwards_are_skipped(profile):
    path = profile("drafts:\n  steps: [records_then_fails]\n")
    ctx, tid = _run(path, first_step="records_then_fails")
    log = ctx.service("store").artifacts
    assert log.list_for_task(tid) == []
    assert [r["type"] for r in log.list_for_task(tid, include_unfinished=True)] == ["master"]


class _Records(Step):
    name = "alpha"

    def run(self, ctx):
        return StepResult(artifacts=[Artifact("master", "m.md", "ab" * 32)])


def test_a_store_without_an_artifact_log_fails_a_step_that_records(tmp_path):
    from tests.conftest import write_config

    ctx = load_context(write_config(tmp_path, steps=("alpha",)))
    registry = StepRegistry()
    registry.register(_Records())
    runner = TaskRunner(
        ctx.repo, ctx.log, ctx.step_runs, registry, ctx.config, notifier=lambda m: None
    )
    tid = ctx.repo.create_task(input="x", workflow="local_file", first_step="alpha", max_retries=0)
    StateMachine(ctx.repo, ctx.log, workflow=ctx.config.workflow("local_file")).claim(
        tid, worker="w"
    )
    runner.execute(tid)
    assert "does not record artifacts" in ctx.repo.get_task(tid)["last_error"]


def _no_paths(ctx, tid, captured):
    task = ctx.repo.get_task(tid)
    (run,) = ctx.step_runs.list_for_task(tid)
    texts = [task["last_error"], run["error"] or "", captured]
    for text in texts:
        assert "私密" not in text and str(ctx.config.workspace_root) not in text, text
    return task["last_error"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads files regardless of permissions")
def test_an_unreadable_artifact_fails_without_its_path_in_errors_or_logs(profile, capsys):
    ctx, tid = _run(profile("drafts:\n  steps: [unreadable]\n"), first_step="unreadable")
    error = _no_paths(ctx, tid, capsys.readouterr().err)
    assert error.endswith("artifact 1 ('master'): cannot read the file (PermissionError)")


def test_a_failing_stat_fails_without_the_path_in_errors_or_logs(profile, capsys, monkeypatch):
    stat = Path.stat

    def denied(self, *args, **kwargs):
        if "私密" in str(self):
            raise PermissionError(13, "Permission denied", str(self))
        return stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", denied)
    ctx, tid = _run(profile("drafts:\n  steps: [unstatable]\n"), first_step="unstatable")
    error = _no_paths(ctx, tid, capsys.readouterr().err)
    assert error.endswith("artifact 1 ('master'): cannot read the file (PermissionError)")


# -- malformed declarations are findings, never "declares nothing" -------------------


@pytest.mark.parametrize(
    "requires, message",
    [
        (123, "must be a list or tuple of types, not int"),
        ({"master"}, "must be a list or tuple of types, not set"),
        ("master", "must be a list or tuple of types, not str"),
        ([1], "artifact type must be a string, got int"),
    ],
)
def test_a_malformed_declaration_raises_an_artifact_type_error(requires, message):
    class Bad(_Plain):
        pass

    Bad.requires = requires
    with pytest.raises(ArtifactTypeError, match=message):
        contract_of(Bad())


def test_a_malformed_declaration_fails_doctor(profile):
    ctx = load_context(profile("drafts:\n  steps: [bad_declaration]\n"))
    check = _doctor(ctx)
    assert check is not None and not check.ok
    assert "'bad_declaration'" in check.detail and "not int" in check.detail


def test_malformed_workflow_inputs_alone_fail_doctor(profile):
    ctx = load_context(profile("drafts:\n  inputs: [Invalid]\n  steps: [undeclared]\n"))
    check = _doctor(ctx)
    assert check is not None and not check.ok
    assert "inputs" in check.detail and "'Invalid'" in check.detail


def test_malformed_inputs_count_as_a_declaration():
    report = check_chain("wf", ["a"], _contracts(), inputs=["Invalid"])
    assert report.declared and not report.ok


# -- when run and the daemon check -------------------------------------------------

GOOD_AND_BROKEN = """
good:
  steps: [fetch, write]
broken:
  steps: [fetch, needs_missing]
"""


def _add(ctx, workflow):
    return ctx.repo.create_task(
        input="in.txt", workflow=workflow, first_step="fetch", max_retries=0
    )


def test_run_only_checks_the_workflows_it_executes(profile):
    ctx = load_context(profile(GOOD_AND_BROKEN))
    tid = _add(ctx, "good")
    run_until_idle(ctx)  # the broken workflow has no task: nothing to refuse
    assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.COMPLETED


def test_run_refuses_the_tasks_of_a_broken_workflow_and_says_why(profile, capsys):
    ctx = load_context(profile(GOOD_AND_BROKEN))
    good, broken = _add(ctx, "good"), _add(ctx, "broken")
    run_until_idle(ctx)
    assert ctx.repo.get_task(good)["task_status"] == TaskStatus.COMPLETED
    task = ctx.repo.get_task(broken)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert task["step"] == "fetch"  # refused before any step of it ran
    assert "artifact contracts of workflow 'broken'" in task["last_error"]
    assert "'needs_missing' requires 'transcript'" in task["last_error"]
    assert ctx.step_runs.list_for_task(broken) == []
    assert f"task #{broken}" in capsys.readouterr().err


def test_the_daemon_checks_each_task_before_running_it(profile):
    ctx = load_context(profile(GOOD_AND_BROKEN))
    daemon = ctx.service("runtime").build_daemon()  # starts despite the broken workflow
    try:
        tid = _add(ctx, "broken")  # added after the daemon started
        daemon.run_once()
        daemon.coordinator.pool.drain()
    finally:
        daemon.coordinator.pool.shutdown(wait=True)
    task = ctx.repo.get_task(tid)
    assert task["task_status"] == TaskStatus.MANUAL_REQUIRED
    assert "artifact contracts of workflow 'broken'" in task["last_error"]


def test_allow_unverified_runs_a_mixed_chain_and_reports_it_once(profile, caplog):
    ctx = load_context(profile("drafts:\n  allow_unverified: true\n  steps: [undeclared, write]\n"))
    first = ctx.repo.create_task(
        input="a", workflow="drafts", first_step="undeclared", max_retries=0
    )
    second = ctx.repo.create_task(
        input="b", workflow="drafts", first_step="undeclared", max_retries=0
    )
    with caplog.at_level("WARNING", logger="runspool.contracts"):
        run_until_idle(ctx)
    for tid in (first, second):
        assert ctx.repo.get_task(tid)["task_status"] == TaskStatus.COMPLETED
    notes = [r.getMessage() for r in caplog.records if r.name == "runspool.contracts"]
    assert len(notes) == 1
    assert "workflow 'drafts'" in notes[0] and "unverified" in notes[0]
