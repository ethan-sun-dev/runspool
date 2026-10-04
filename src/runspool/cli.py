"""Runspool command line: task lifecycle, one-shot run, daemon, and JSON output."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from typing import Any

import typer

from runspool import commands
from runspool.app import DEFAULT_CONFIG_FILENAME, init_app, load_context
from runspool.daemon import write_pid
from runspool.display import (
    format_logs,
    format_overview,
    format_task_detail,
    format_task_list,
)
from runspool.doctor import run_doctor
from runspool.kernel import ConfigError, KernelError
from runspool.persistence.state_machine import IllegalTransition
from runspool.runtime import (
    build_daemon,
    daemon_pid_file,
    daemon_status,
    request_daemon_stop,
    run_until_idle,
)
from runspool.views import detail_view, events_view, inspect_view, list_view, overview_view

app = typer.Typer(
    help="Runspool: a local-first CLI workflow engine for reliable personal automation.",
    no_args_is_help=True,
)

_STATE: dict[str, Any] = {"config_path": Path(DEFAULT_CONFIG_FILENAME), "booted": None}


@app.callback()
def root(
    config_path: Path = typer.Option(
        Path(DEFAULT_CONFIG_FILENAME), "--config-path", "-c", help="Path to the config file."
    ),
) -> None:
    _STATE["config_path"] = config_path


def _ctx():
    path = Path(_STATE["config_path"])
    booted = _STATE.get("booted")
    if booted is not None and booted[0] == path:
        return booted[1]  # booted by main() to collect plugin commands; reuse it
    failed = _STATE.get("boot_error")
    if failed is not None and failed[0] == path:
        _fail(failed[1])  # main() already tried this profile: report, don't boot twice
    try:
        return load_context(path)
    except (KernelError, ConfigError) as exc:  # a required plugin failed, or bad config
        _fail(exc)


def _fail(exc: Exception):
    typer.echo(f"error: {exc}", err=True)
    raise typer.Exit(1) from exc


def _emit_json(obj: Any) -> None:
    typer.echo(json.dumps(obj, indent=2, ensure_ascii=False))


def _guard(action, ok_msg: str) -> None:
    """Run a control action, turning KeyError (missing task/workflow) and
    ValueError (bad argument) into clean messages and a non-zero exit code."""
    try:
        action()
    except KeyError as exc:
        typer.echo(f"not found: {exc.args[0] if exc.args else exc}")
        raise typer.Exit(1) from exc
    except (ValueError, IllegalTransition) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc
    typer.echo(ok_msg)


@app.command()
def init(
    workspace_root: Path = typer.Option(
        Path("./workspace"), help="Workspace root directory."
    ),
) -> None:
    """Generate a config (without overwriting an existing one) and init the database."""
    try:
        created = init_app(_STATE["config_path"], workspace_root=workspace_root)
    except KernelError as exc:
        _fail(exc)
    if created:
        typer.echo(f"Created {_STATE['config_path']} and initialised the database.")
    else:
        typer.echo("Config already exists; database is initialised.")


@app.command()
def add(
    input: str = typer.Argument(..., help="The task input (e.g. a file path)."),
    workflow: str = typer.Option("local_file", "--workflow", "-w", help="Workflow name."),
    name: str = typer.Option(None, "--name", help="Human-readable task name."),
    force: bool = typer.Option(False, "--force", help="Allow a duplicate active task."),
    parent: int = typer.Option(None, "--parent", help="Id of the task this one derives from."),
    meta: list[str] = typer.Option(
        None, "--meta", help="Metadata as KEY=VALUE (repeatable)."
    ),
) -> None:
    """Add a task for an input."""
    metadata: dict[str, str] = {}
    for item in meta or []:
        key, sep, value = item.partition("=")
        if not sep or not key:
            typer.echo(f"--meta expects KEY=VALUE, got {item!r}")
            raise typer.Exit(1)
        metadata[key] = value
    try:
        task_id = commands.add_task(
            _ctx(),
            input,
            workflow=workflow,
            name=name,
            force=force,
            metadata=metadata or None,
            parent_task_id=parent,
        )
    except commands.DuplicateTaskError as exc:
        typer.echo(f"active task already exists: {exc.existing_id} (use --force to add anyway)")
        raise typer.Exit(1) from exc
    except KeyError as exc:
        missing = exc.args[0] if exc.args else exc
        what = "parent task" if missing == parent else "workflow"
        typer.echo(f"undefined {what}: {missing}")
        raise typer.Exit(1) from exc
    typer.echo(f"Created task {task_id}")


@app.command()
def run(
    json_output: bool = typer.Option(False, "--json", help="Emit a JSON summary."),
    force: bool = typer.Option(False, "--force", help="Run even if a daemon is active."),
) -> None:
    """Advance every runnable task once, until no further progress is made."""
    ctx = _ctx()
    # Refuse to run alongside a live daemon: run recovers all RUNNING tasks back
    # to queued, which would yank the daemon's in-flight tasks and execute the
    # same step twice. --force overrides for the rare deliberate case.
    if not force and daemon_status(ctx)["running"]:
        typer.echo("daemon is running; refusing to run (use --force to override)")
        raise typer.Exit(1)
    # In JSON mode, keep stdout a single clean JSON document by silencing the
    # per-step notifier entirely (default would otherwise print to stderr).
    notifier = (lambda m: None) if json_output else (lambda m: typer.echo(m))
    try:
        rounds = run_until_idle(ctx, notifier=notifier)
    except KernelError as exc:  # steps unavailable because a plugin failed to load
        _fail(exc)
    if json_output:
        _emit_json({"rounds": rounds, "tasks": list_view(ctx.repo.list_all())})
    else:
        typer.echo(f"Done ({rounds} round(s)).")
        typer.echo(format_overview(ctx.repo))


@app.command()
def status(
    task_id: int = typer.Argument(None, help="Task id; omit to list all tasks."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show a task's detail, or list all tasks."""
    ctx = _ctx()
    if task_id is None:
        if json_output:
            _emit_json(list_view(ctx.repo.list_all()))
        else:
            typer.echo(format_task_list(ctx.repo))
        return
    task = ctx.repo.get_task(task_id)
    if task is None:
        if json_output:
            _emit_json({"error": "not_found", "id": task_id})
        else:
            typer.echo(f"task {task_id} not found")
        raise typer.Exit(1)
    if json_output:
        _emit_json(detail_view(ctx, task))
    else:
        ordered = None
        try:
            ordered = ctx.config.workflow(task["workflow"]).steps
        except KeyError:
            ordered = None
        runs = ctx.step_runs.list_for_task(task_id)
        typer.echo(format_task_detail(ctx.repo, ctx.log, task_id, runs=runs, ordered_steps=ordered))


@app.command()
def overview(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Summarise task counts by status."""
    ctx = _ctx()
    if json_output:
        _emit_json(overview_view(ctx))
    else:
        typer.echo(format_overview(ctx.repo))


@app.command()
def logs(
    task_id: int = typer.Argument(...),
    limit: int = typer.Option(20, help="Most recent N events."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show a task's event log."""
    ctx = _ctx()
    # Distinguish "no such task" from "task exists but has no events": an empty
    # list could otherwise be misread (especially by an agent) as the latter.
    # Mirror status/inspect, which return not_found + exit 1 for a missing id.
    if ctx.repo.get_task(task_id) is None:
        if json_output:
            _emit_json({"error": "not_found", "id": task_id})
        else:
            typer.echo(f"task {task_id} not found")
        raise typer.Exit(1)
    if json_output:
        _emit_json(events_view(ctx.log.list_for_task(task_id, limit=limit)))
    else:
        typer.echo(format_logs(ctx.log, task_id, limit=limit))


@app.command()
def inspect(
    task_id: int = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show an agent-friendly snapshot with available actions and a suggestion."""
    ctx = _ctx()
    task = ctx.repo.get_task(task_id)
    if task is None:
        if json_output:
            _emit_json({"error": "not_found", "id": task_id})
        else:
            typer.echo(f"task {task_id} not found")
        raise typer.Exit(1)
    view = inspect_view(ctx, task)
    if json_output:
        _emit_json(view)
        return
    typer.echo(f"Task #{view['id']} ({view['name'] or 'unnamed'})")
    typer.echo(f"  Status:       {view['status']}")
    typer.echo(f"  Workflow:     {view['workflow']}")
    typer.echo(f"  Current step: {view['current_step']}")
    if view["last_error"]:
        typer.echo(f"  Last error:   {view['last_error']}")
    if view["artifacts"]:
        typer.echo(f"  Artifacts:    {len(view['artifacts'])}")
        for a in view["artifacts"]:
            typer.echo(f"    - {a}")
    typer.echo(f"  Actions:      {', '.join(view['available_actions']) or '(none)'}")
    typer.echo(f"  Next:         {view['suggested_next_action']}")


@app.command()
def pause(task_id: int = typer.Argument(...)) -> None:
    """Request a pause (applied after the current step finishes)."""
    _guard(lambda: commands.pause_task(_ctx(), task_id), f"Requested pause for task {task_id}")


@app.command()
def resume(task_id: int = typer.Argument(...)) -> None:
    """Resume a paused task."""
    _guard(lambda: commands.resume_task(_ctx(), task_id), f"Resumed task {task_id}")


@app.command()
def terminate(task_id: int = typer.Argument(...)) -> None:
    """Request termination (applied after the current step finishes)."""
    _guard(
        lambda: commands.terminate_task(_ctx(), task_id),
        f"Requested termination for task {task_id}",
    )


@app.command()
def retry(task_id: int = typer.Argument(...)) -> None:
    """Requeue a failed or manual_required task from its current step."""
    _guard(lambda: commands.retry_task(_ctx(), task_id), f"Requeued task {task_id}")


@app.command()
def approve(task_id: int = typer.Argument(...)) -> None:
    """Approve the step a task is waiting on (that one attempt), and requeue it."""
    _guard(
        lambda: commands.approve_task(_ctx(), task_id, by=_who()),
        f"Approved task {task_id}",
    )


@app.command()
def reject(
    task_id: int = typer.Argument(...),
    reason: str = typer.Option("", "--reason", help="Why; kept in the task's log."),
) -> None:
    """Refuse the step a task is waiting on; the task then needs attention."""
    _guard(
        lambda: commands.reject_task(_ctx(), task_id, reason=reason, by=_who()),
        f"Rejected task {task_id}",
    )


def _who() -> str:
    import getpass

    try:
        return f"cli:{getpass.getuser()}"
    except Exception:  # noqa: BLE001 - no user name is not a reason to fail
        return "cli"


@app.command()
def wake(task_id: int = typer.Argument(...)) -> None:
    """Run a deferred task now instead of waiting out its delay."""
    _guard(lambda: commands.wake_task(_ctx(), task_id), f"Woke task {task_id}")


@app.command(name="set-priority")
def set_priority_cmd(
    task_id: int = typer.Argument(...), priority: int = typer.Argument(...)
) -> None:
    """Set a task's scheduling priority (higher runs first)."""
    _guard(
        lambda: commands.set_priority(_ctx(), task_id, priority),
        f"task {task_id} priority={priority}",
    )


@app.command(name="set-retries")
def set_retries_cmd(
    task_id: int = typer.Argument(...), max_retries: int = typer.Argument(...)
) -> None:
    """Set a task's retry ceiling and reset its retry count."""
    _guard(
        lambda: commands.set_retries(_ctx(), task_id, max_retries),
        f"task {task_id} max_retries={max_retries}",
    )


@app.command(name="set-step")
def set_step_cmd(
    task_id: int = typer.Argument(...),
    step: str = typer.Argument(...),
    force: bool = typer.Option(
        False, "--force", help="Allow moving a task that is not failed/manual_required."
    ),
) -> None:
    """Move a task to a specific step in its workflow."""
    _guard(
        lambda: commands.set_step(_ctx(), task_id, step, force=force),
        f"task {task_id} step={step}",
    )


@app.command()
def workflows(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """List defined workflows and their steps."""
    ctx = _ctx()
    if json_output:
        _emit_json({name: list(wf.steps) for name, wf in ctx.config.workflows.items()})
        return
    for name, wf in ctx.config.workflows.items():
        typer.echo(f"{name}: {' -> '.join(wf.steps)}")


@app.command()
def doctor(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Check the local environment."""
    checks = run_doctor(_ctx())
    if json_output:
        _emit_json([{"name": c.name, "ok": c.ok, "detail": c.detail} for c in checks])
    else:
        for c in checks:
            mark = "OK " if c.ok else "BAD"
            typer.echo(f"[{mark}] {c.name}: {c.detail}")
    if not all(c.ok for c in checks):
        raise typer.Exit(1)  # so scripts and agents can act on the result


@app.command(name="daemon-status")
def daemon_status_cmd(json_output: bool = typer.Option(False, "--json", help="Emit JSON.")) -> None:
    """Report whether the daemon is running."""
    s = daemon_status(_ctx())
    if json_output:
        _emit_json(s)
        return
    typer.echo(f"daemon running (pid={s['pid']})" if s["running"] else "daemon not running")


@app.command(name="daemon-stop")
def daemon_stop_cmd() -> None:
    """Signal a running daemon to stop."""
    typer.echo("Requested daemon stop" if request_daemon_stop(_ctx()) else "daemon not running")


@app.command()
def daemon() -> None:
    """Run the daemon in the foreground (Ctrl-C to exit)."""
    ctx = _ctx()
    if daemon_status(ctx)["running"]:
        typer.echo("daemon already running; not starting another")
        raise typer.Exit(1)
    try:
        d = build_daemon(ctx)
    except KernelError as exc:
        _fail(exc)
    pid_file = daemon_pid_file(ctx)
    write_pid(pid_file, os.getpid())

    def _handle(signum, frame):
        d.request_stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)
    typer.echo("daemon started (Ctrl-C to exit)")
    try:
        d.run()
    finally:
        pid_file.unlink(missing_ok=True)
        if ctx.kernel is not None:
            ctx.kernel.dispose()  # let plugins release threads, sockets, files


def _config_path_from(argv: list[str]) -> Path:
    """The profile path among the global options (those before the command)."""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-c", "--config-path") and i + 1 < len(argv):
            return Path(argv[i + 1])
        if arg.startswith("--config-path="):
            return Path(arg.split("=", 1)[1])
        if arg.startswith("-c") and not arg.startswith("--") and len(arg) > 2:
            return Path(arg[2:])  # click's attached form: -cprofile.yaml
        if not arg.startswith("-"):
            break  # the command: options after it are the command's, not global
        i += 1
    return Path(DEFAULT_CONFIG_FILENAME)


def build_app(config_path: Path | str) -> typer.Typer:
    """The CLI for a profile: the built-in commands plus those its plugins register.

    Boots the profile (and keeps the context for the command to reuse). If it does
    not boot, only the built-in commands are offered; the command then reports why.
    """
    combined = typer.Typer(help=app.info.help, no_args_is_help=True)
    combined.registered_callback = app.registered_callback
    combined.registered_commands = list(app.registered_commands)
    combined.registered_groups = list(app.registered_groups)
    path = Path(config_path)
    _STATE["booted"] = None
    _STATE["boot_error"] = None
    if not path.exists():
        return combined
    try:
        ctx = load_context(path)
    except Exception as exc:  # noqa: BLE001 - reported by the command that needs it
        _STATE["boot_error"] = (path, exc)
        return combined
    _STATE["booted"] = (path, ctx)
    builtin = _builtin_names()
    try:
        contributed_commands = ctx.service("cli").commands
    except LookupError:  # the cli entry is disabled: built-in commands only
        contributed_commands = []
    for contributed in contributed_commands:
        if contributed.name in builtin:
            typer.echo(
                f"warning: plugin {contributed.owner!r} command {contributed.name!r} "
                "clashes with a built-in command and is ignored",
                err=True,
            )
            continue
        if isinstance(contributed.command, typer.Typer):
            combined.add_typer(contributed.command, name=contributed.name)
        else:
            combined.command(name=contributed.name)(contributed.command)
    return combined


def main() -> None:
    """Console entry point: ``runspool``."""
    import sys

    argv = sys.argv[1:]
    cli = build_app(_config_path_from(argv))
    failed = _STATE.get("boot_error")
    error = failed[1] if failed is not None else None
    command = _command_in(argv)
    if error is not None and command and command not in _builtin_names():
        # Probably a plugin command that did not load: say why, not "No such command".
        typer.echo(
            f"error: {command!r} is unavailable: the profile did not load: {error}", err=True
        )
        raise SystemExit(1)
    try:
        cli()
    finally:
        # Let plugins release what they hold (threads, sockets, files) when the
        # command ends, not only when a daemon stops.
        booted = _STATE.get("booted")
        if booted is not None and booted[1].kernel is not None:
            booted[1].kernel.dispose()


def _builtin_names() -> set[str]:
    return {c.name or c.callback.__name__.replace("_", "-") for c in app.registered_commands}


def _command_in(argv: list[str]) -> str | None:
    skip = False
    for arg in argv:
        if skip:
            skip = False
            continue
        if arg in ("-c", "--config-path"):
            skip = True
            continue
        if not arg.startswith("-"):
            return arg
    return None


if __name__ == "__main__":
    main()
