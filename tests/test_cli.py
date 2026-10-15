"""CLI integration tests via Typer's CliRunner."""

import json
import signal
import time

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


def test_set_retries_rejects_negative(tmp_path):
    cfg = _init(tmp_path)
    src = tmp_path / "a.txt"
    src.write_text("hello world", encoding="utf-8")
    _invoke(cfg, "add", str(src))

    # A negative cap is refused (mirrors the config model's ge=0 constraint).
    bad = _invoke(cfg, "set-retries", "1", "--", "-5")
    assert bad.exit_code == 1
    assert "max-retries must be >= 0" in bad.output
    # The task's cap is left untouched.
    status = json.loads(_invoke(cfg, "status", "1", "--json").output)
    assert status["max_retries"] >= 0

    # A valid value still works.
    assert _invoke(cfg, "set-retries", "1", "3").exit_code == 0


def test_logs_missing_task_reports_not_found(tmp_path):
    cfg = _init(tmp_path)
    # Human output: clear message + non-zero exit (consistent with status/inspect).
    human = _invoke(cfg, "logs", "999")
    assert human.exit_code == 1
    assert "not found" in human.output
    # JSON output: a not_found error object, not an empty list that an agent
    # could misread as "task exists but has no events".
    js = _invoke(cfg, "logs", "999", "--json")
    assert js.exit_code == 1
    assert json.loads(js.output) == {"error": "not_found", "id": 999}


def test_add_unknown_workflow(tmp_path):
    cfg = _init(tmp_path)
    result = _invoke(cfg, "add", "x", "--workflow", "nope")
    assert result.exit_code == 1
    assert result.output.strip() == "undefined workflow: nope"


def test_wake_runs_a_deferred_task_now(tmp_path):
    from runspool.app import load_context
    from tests.support import force_fields

    cfg = _init(tmp_path)
    src = tmp_path / "a.txt"
    src.write_text("hello world", encoding="utf-8")
    _invoke(cfg, "add", str(src))
    assert _invoke(cfg, "wake", "1").exit_code == 1  # not waiting on anything
    force_fields(load_context(cfg).repo, 1, {"next_retry_at": "2999-01-01 00:00:00"})
    woke = _invoke(cfg, "wake", "1")
    assert woke.exit_code == 0, woke.output
    assert json.loads(_invoke(cfg, "status", "1", "--json").output)["next_retry_at"] is None


def test_add_a_sub_task_with_parent_and_metadata(tmp_path):
    cfg = _init(tmp_path)
    src = tmp_path / "a.txt"
    src.write_text("hello world", encoding="utf-8")
    _invoke(cfg, "add", str(src))
    child = _invoke(
        cfg, "add", "wechat:1", "--parent", "1", "--meta", "regen=yes", "--meta", "lang=zh"
    )
    assert child.exit_code == 0, child.output
    view = json.loads(_invoke(cfg, "inspect", "2", "--json").output)
    assert (view["parent_task_id"], view["metadata"]) == (1, {"regen": "yes", "lang": "zh"})
    missing = _invoke(cfg, "add", "x", "--parent", "99")
    assert missing.exit_code == 1 and "undefined parent task: 99" in missing.output
    bad = _invoke(cfg, "add", "y", "--meta", "novalue")
    assert bad.exit_code == 1 and "KEY=VALUE" in bad.output


def test_first_task_id_continues_an_older_numbering(tmp_path):
    cfg = _init(tmp_path)
    cfg.write_text(cfg.read_text(encoding="utf-8") + "\nfirst_task_id: 700\n", encoding="utf-8")
    src = tmp_path / "a.txt"
    src.write_text("hello world", encoding="utf-8")
    assert "Created task 700" in _invoke(cfg, "add", str(src)).output


def test_the_example_config_loads(tmp_path):
    from pathlib import Path

    from runspool.core.boot import load_profile

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    copy = tmp_path / "config.yaml"
    copy.write_text(example.read_text(encoding="utf-8"), encoding="utf-8")
    load_profile(copy)  # raises if the shipped example is invalid


def test_doctor_exits_non_zero_when_a_check_fails(tmp_path):
    cfg = _init(tmp_path)
    cfg.write_text(
        cfg.read_text(encoding="utf-8")
        + "\nworkflows:\n  broken:\n    steps: [no_such_step]\n",
        encoding="utf-8",
    )
    result = _invoke(cfg, "doctor", "--json")
    assert result.exit_code == 1
    assert any(not c["ok"] for c in json.loads(result.output))


def test_bad_engine_settings_are_reported_without_echoing_values(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        f"workspace_root: {tmp_path / 'ws'}\nscheduler:\n  max_retries: sekrit-not-a-number\n",
        encoding="utf-8",
    )
    result = _invoke(cfg, "status")
    assert result.exit_code == 1
    assert "max_retries" in result.output and "sekrit" not in result.output
    assert "Traceback" not in result.output


def _profile_with_default(tmp_path, default):
    cfg = tmp_path / "runspool.yaml"
    cfg.write_text(
        f"workspace_root: {tmp_path / 'ws'}\n"
        f"default_workflow: {default}\n"
        "workflows:\n  notes:\n    steps: [ingest_file, archive]\n",
        encoding="utf-8",
    )
    return cfg


def test_add_uses_the_profile_default_workflow(tmp_path):
    cfg = _profile_with_default(tmp_path, "notes")
    src = tmp_path / "a.txt"
    src.write_text("hi", encoding="utf-8")
    assert _invoke(cfg, "add", str(src)).exit_code == 0
    assert json.loads(_invoke(cfg, "status", "1", "--json").output)["workflow"] == "notes"
    doctor = _invoke(cfg, "doctor")
    assert "default: notes" in doctor.output


def test_an_undefined_default_workflow_is_reported(tmp_path):
    cfg = _profile_with_default(tmp_path, "nope")
    src = tmp_path / "a.txt"
    src.write_text("hi", encoding="utf-8")
    result = _invoke(cfg, "add", str(src))
    assert result.exit_code == 1 and "undefined workflow: nope" in result.output
    doctor = _invoke(cfg, "doctor")
    assert doctor.exit_code == 1
    assert "default_workflow 'nope' is not defined" in doctor.output


def test_without_a_default_add_still_uses_local_file(tmp_path):
    cfg = _init(tmp_path)
    src = tmp_path / "a.txt"
    src.write_text("hi", encoding="utf-8")
    assert _invoke(cfg, "add", str(src)).exit_code == 0
    assert json.loads(_invoke(cfg, "status", "1", "--json").output)["workflow"] == "local_file"


def test_an_empty_workflow_option_is_an_error_not_the_default(tmp_path):
    cfg = _profile_with_default(tmp_path, "notes")
    src = tmp_path / "a.txt"
    src.write_text("hi", encoding="utf-8")
    result = _invoke(cfg, "add", str(src), "--workflow", "")
    assert result.exit_code == 1
    assert result.output.strip() == "undefined workflow:"


def _many_tasks(tmp_path, n):
    from runspool.app import load_context
    from tests.support import force_fields

    cfg = _init(tmp_path)
    ctx = load_context(cfg)
    for i in range(1, n + 1):
        ctx.repo.create_task(input=f"in-{i}", workflow="local_file", first_step="ingest_file",
                             max_retries=1)  # fmt: skip
    for tid in range(1, n + 1):
        if tid not in (2, 3):  # 2、3 留着没完成
            force_fields(ctx.repo, tid, {"task_status": "completed"})
    force_fields(ctx.repo, 3, {"task_status": "manual_required"})
    return cfg


def _ids(output):
    import re

    return [int(m.group(1)) for m in re.finditer(r"^#\s*(\d+)", output, flags=re.M)]


def test_status_lists_the_newest_20_and_points_at_hidden_unfinished_ones(tmp_path):
    cfg = _many_tasks(tmp_path, 30)
    out = _invoke(cfg, "status").output
    assert _ids(out) == list(range(30, 10, -1))
    assert "showing 20 of 30" in out
    assert "2 unfinished not shown (#3, #2); --active lists them" in out


def test_status_limit_all_active_and_status_filters(tmp_path):
    cfg = _many_tasks(tmp_path, 30)
    assert _ids(_invoke(cfg, "status", "-n", "5").output) == [30, 29, 28, 27, 26]
    assert len(_ids(_invoke(cfg, "status", "--all").output)) == 30
    assert "showing" not in _invoke(cfg, "status", "--all").output
    assert _ids(_invoke(cfg, "status", "--active").output) == [3, 2]
    assert _ids(_invoke(cfg, "status", "--status", "manual_required").output) == [3]
    both = _invoke(cfg, "status", "--status", "manual_required", "--status", "queued")
    assert _ids(both.output) == [3, 2]
    bad = _invoke(cfg, "status", "--status", "nope")
    assert bad.exit_code == 1 and "unknown status: nope" in bad.output
    mixed = _invoke(cfg, "status", "3", "--active")
    assert mixed.exit_code == 1


def test_status_json_still_lists_everything_unless_asked(tmp_path):
    cfg = _many_tasks(tmp_path, 30)
    everything = json.loads(_invoke(cfg, "status", "--json").output)
    assert [t["id"] for t in everything] == list(range(1, 31))
    newest = json.loads(_invoke(cfg, "status", "--json", "-n", "3").output)
    assert [t["id"] for t in newest] == [28, 29, 30]
    active = json.loads(_invoke(cfg, "status", "--json", "--active").output)
    assert [t["id"] for t in active] == [2, 3]


def test_detach_stdin_points_fd0_at_dev_null():
    """Children of the daemon must not inherit a terminal as stdin (ffmpeg reads it, and
    a background job that reads the terminal is stopped by SIGTTIN, daemon included)."""
    import subprocess
    import sys

    code = (
        "import os; from runspool.cli import _detach_stdin; _detach_stdin(); "
        "import subprocess, sys; "
        "r = subprocess.run([sys.executable, '-c', "
        "'import os; print(os.path.samestat(os.fstat(0), os.stat(os.devnull)))'], "
        "capture_output=True, text=True); print(r.stdout.strip())"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], stdin=subprocess.PIPE, capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "True"  # the grandchild sees /dev/null, not the pipe


def test_the_daemon_command_detaches_stdin(tmp_path, monkeypatch):
    import runspool.cli as cli

    cfg = _init(tmp_path)
    calls = []
    monkeypatch.setattr(cli, "_detach_stdin", lambda: calls.append("detached"))

    class _Daemon:
        def request_stop(self):
            pass

        def run(self):
            calls.append("run")

    monkeypatch.setattr(cli, "build_daemon", lambda ctx: _Daemon())
    result = _invoke(cfg, "daemon")
    assert result.exit_code == 0, result.output
    assert calls == ["detached", "run"]


def test_daemon_status_reports_a_suspended_daemon(tmp_path, monkeypatch):
    """A daemon stopped by SIGTTIN looks alive (its pid exists) but schedules nothing."""
    import os

    import runspool.core.runtime as rt
    from runspool.app import load_context

    cfg = _init(tmp_path)
    pid_file = load_context(cfg).service("runtime").pid_file()
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()), encoding="utf-8")  # an alive pid

    monkeypatch.setattr(rt, "_pid_state", lambda pid: "S")
    ok = _invoke(cfg, "daemon-status")
    assert ok.exit_code == 0 and ok.output.startswith("daemon running")
    assert json.loads(_invoke(cfg, "daemon-status", "--json").output)["stopped"] is False

    monkeypatch.setattr(rt, "_pid_state", lambda pid: "T")
    stopped = _invoke(cfg, "daemon-status")
    assert stopped.exit_code == 1
    assert "STOPPED" in stopped.output and "kill -CONT -" in stopped.output
    data = json.loads(_invoke(cfg, "daemon-status", "--json").output)
    assert data == {"running": True, "pid": os.getpid(), "stopped": True}


def test_pid_state_reads_a_real_process():
    import os
    import subprocess
    import sys

    from runspool.core.runtime import _pid_state

    assert _pid_state(os.getpid()) in ("R", "S", "U", "D")
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        os.kill(child.pid, signal.SIGSTOP)
        for _ in range(50):
            if _pid_state(child.pid) == "T":
                break
            time.sleep(0.05)
        assert _pid_state(child.pid) == "T"
    finally:
        os.kill(child.pid, signal.SIGKILL)
        child.wait()
