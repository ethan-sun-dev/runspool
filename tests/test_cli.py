"""CLI integration tests via Typer's CliRunner."""

import json

from typer.testing import CliRunner

from runspool.cli import app

runner = CliRunner()


def _invoke(config_path, *args):
    return runner.invoke(app, ["-c", str(config_path), *args])


def _init(tmp_path):
    cfg = tmp_path / "config.yaml"
    result = runner.invoke(
        app, ["-c", str(cfg), "init", "--workspace-root", str(tmp_path / "ws")]
    )
    assert result.exit_code == 0, result.output
    return cfg


def test_init_creates_config(tmp_path):
    cfg = _init(tmp_path)
    assert cfg.exists()


def test_add_run_status_inspect_flow(tmp_path):
    cfg = _init(tmp_path)
    src = tmp_path / "notes.txt"
    src.write_text("Meeting notes: agenda and action items discussed.", encoding="utf-8")

    add = _invoke(cfg, "add", str(src), "--workflow", "local_file")
    assert add.exit_code == 0
    assert "Created task 1" in add.output

    run = _invoke(cfg, "run")
    assert run.exit_code == 0

    # status --json (list)
    s = _invoke(cfg, "status", "--json")
    tasks = json.loads(s.output)
    assert tasks[0]["task_status"] == "completed"

    # inspect --json
    insp = _invoke(cfg, "inspect", "1", "--json")
    view = json.loads(insp.output)
    assert view["id"] == 1
    assert view["status"] == "completed"
    assert any(a.endswith("summary.md") for a in view["artifacts"])
    assert view["available_actions"] == []


def test_overview_and_workflows_json(tmp_path):
    cfg = _init(tmp_path)
    ov = _invoke(cfg, "overview", "--json")
    assert json.loads(ov.output) == {}  # no tasks yet

    wf = _invoke(cfg, "workflows", "--json")
    data = json.loads(wf.output)
    assert data["local_file"][0] == "ingest_file"


def test_doctor_json_all_ok(tmp_path):
    cfg = _init(tmp_path)
    result = _invoke(cfg, "doctor", "--json")
    checks = json.loads(result.output)
    names = {c["name"] for c in checks}
    assert {"python", "workspace_root", "database", "workflows", "steps"} <= names
    assert all(c["ok"] for c in checks)


def test_lifecycle_controls(tmp_path):
    cfg = _init(tmp_path)
    src = tmp_path / "a.txt"
    src.write_text("hello world", encoding="utf-8")
    _invoke(cfg, "add", str(src))

    # pause -> resume
    assert _invoke(cfg, "pause", "1").exit_code == 0
    assert _invoke(cfg, "resume", "1").exit_code == 0

    # set-priority
    assert _invoke(cfg, "set-priority", "1", "5").exit_code == 0

    # unknown task -> non-zero exit
    missing = _invoke(cfg, "pause", "999")
    assert missing.exit_code == 1


def test_add_duplicate_blocked_without_force(tmp_path):
    cfg = _init(tmp_path)
    src = tmp_path / "dup.txt"
    src.write_text("x", encoding="utf-8")
    assert _invoke(cfg, "add", str(src)).exit_code == 0
    dup = _invoke(cfg, "add", str(src))
    assert dup.exit_code == 1
    assert "already exists" in dup.output
    assert _invoke(cfg, "add", str(src), "--force").exit_code == 0


def test_control_commands_rejected_on_completed_task(tmp_path):
    cfg = _init(tmp_path)
    src = tmp_path / "done.txt"
    src.write_text("hello world", encoding="utf-8")
    _invoke(cfg, "add", str(src))
    _invoke(cfg, "run")

    # Task is completed; control actions must be refused and leave it untouched.
    for cmd in (["retry", "1"], ["pause", "1"], ["resume", "1"], ["terminate", "1"]):
        result = _invoke(cfg, *cmd)
        assert result.exit_code == 1, f"{cmd} should be rejected"

    status = json.loads(_invoke(cfg, "status", "1", "--json").output)
    assert status["task_status"] == "completed"


def test_set_step_requires_force_outside_recovery(tmp_path):
    cfg = _init(tmp_path)
    src = tmp_path / "x.txt"
    src.write_text("hello", encoding="utf-8")
    _invoke(cfg, "add", str(src))  # queued

    blocked = _invoke(cfg, "set-step", "1", "archive")
    assert blocked.exit_code == 1
    forced = _invoke(cfg, "set-step", "1", "archive", "--force")
    assert forced.exit_code == 0


def test_run_refused_while_daemon_running(tmp_path, monkeypatch):
    cfg = _init(tmp_path)
    src = tmp_path / "a.txt"
    src.write_text("hello world", encoding="utf-8")
    _invoke(cfg, "add", str(src))

    # Simulate a live daemon: run must refuse so it cannot yank the daemon's
    # in-flight tasks back to queued and double-execute them.
    monkeypatch.setattr("runspool.cli.daemon_status", lambda ctx: {"running": True, "pid": 4321})
    blocked = _invoke(cfg, "run")
    assert blocked.exit_code == 1
    assert "daemon" in blocked.output.lower()

    # The task must be left untouched (still queued, not recovered/advanced).
    status = json.loads(_invoke(cfg, "status", "1", "--json").output)
    assert status["task_status"] == "queued"

    # --force overrides the guard for the rare deliberate case.
    forced = _invoke(cfg, "run", "--force")
    assert forced.exit_code == 0


def test_add_unknown_workflow(tmp_path):
    cfg = _init(tmp_path)
    result = _invoke(cfg, "add", "x", "--workflow", "nope")
    assert result.exit_code == 1
    assert "undefined workflow" in result.output
