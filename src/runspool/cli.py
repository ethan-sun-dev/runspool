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
    DEFAULT_LIST_LIMIT,
    format_logs,
    format_overview,
    format_task_detail,
    format_task_list,
    select_tasks,
)
from runspool.doctor import run_doctor
from runspool.executor_lock import ExecutorBusy
from runspool.kernel import ConfigError, KernelError
from runspool.models import TaskStatus
from runspool.persistence.state_machine import IllegalTransition
from runspool.runtime import (
    build_daemon,
    daemon_pid_file,
    daemon_status,
    request_daemon_stop,
    run_until_idle,
)
from runspool.views import (
    artifacts_view,
    detail_view,
    event_feed_view,
    events_view,
    inspect_view,
    item_view,
    list_view,
    overview_view,
)

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
    workflow: str = typer.Option(
        None,
        "--workflow",
        "-w",
        help="Workflow name (default: the profile's default_workflow, else local_file).",
    ),
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
    ctx = _ctx()
    if workflow is None:  # only when -w is absent: an empty -w "" is still an error
        workflow = ctx.config.add_workflow()
    if workflow not in ctx.config.workflows:
        typer.echo(f"undefined workflow: {workflow}")
        raise typer.Exit(1)
    try:
        task_id = commands.add_task(
            ctx,
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
        if parent is not None and missing == parent:
            typer.echo(f"undefined parent task: {parent}")
        else:
            typer.echo(f"error: missing {missing!r}")
        raise typer.Exit(1) from exc
    typer.echo(f"Created task {task_id}")


@app.command()
def run(
    json_output: bool = typer.Option(False, "--json", help="Emit a JSON summary."),
    force: bool = typer.Option(
        False,
        "--force",
        help="Skip the daemon pid check (the executor lock still applies).",
    ),
) -> None:
    """Advance every runnable task once, until no further progress is made."""
    ctx = _ctx()
    # Refuse to run alongside a live daemon: run recovers all RUNNING tasks back
    # to queued, which would yank the daemon's in-flight tasks and execute the
    # same step twice. The executor lock enforces this for any process that takes
    # it; the pid check also covers a daemon from a release before the lock.
    if not force and daemon_status(ctx)["running"]:
        typer.echo("daemon is running; refusing to run (use --force to override)")
        raise typer.Exit(1)
    # In JSON mode, keep stdout a single clean JSON document by silencing the
    # per-step notifier entirely (default would otherwise print to stderr).
    notifier = (lambda m: None) if json_output else (lambda m: typer.echo(m))
    try:
        rounds = run_until_idle(ctx, notifier=notifier)
    except ExecutorBusy as exc:  # a daemon or another run is executing tasks
        _fail(exc)
    except KernelError as exc:  # steps unavailable because a plugin failed to load
        _fail(exc)
    if json_output:
        _emit_json({"rounds": rounds, "tasks": list_view(ctx.repo.list_all())})
    else:
        typer.echo(f"Done ({rounds} round(s)).")
        typer.echo(format_overview(ctx.repo))


@app.command()
def status(
    task_id: int = typer.Argument(None, help="Task id; omit to list tasks."),
    limit: int = typer.Option(
        None,
        "--limit",
        "-n",
        min=1,
        help=f"Newest N tasks (default {DEFAULT_LIST_LIMIT}; JSON: all).",
    ),
    all_tasks: bool = typer.Option(False, "--all", help="List every task."),
    active: bool = typer.Option(
        False, "--active", help="Only unfinished tasks (queued, running, paused, failed, ...)."
    ),
    status_filter: list[str] = typer.Option(
        None, "--status", help="Only tasks in this status (repeatable)."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show a task's detail, or list tasks (newest first; the latest 20 by default)."""
    statuses = list(status_filter or [])
    unknown = sorted(set(statuses) - {s.value for s in TaskStatus})
    if unknown:
        typer.echo(
            f"unknown status: {', '.join(unknown)} "
            f"(one of: {', '.join(s.value for s in TaskStatus)})"
        )
        raise typer.Exit(1)
    if task_id is not None and (limit or all_tasks or active or statuses):
        typer.echo("--limit / --all / --active / --status apply to the task list, not to one task")
        raise typer.Exit(1)
    ctx = _ctx()
    if task_id is None:
        if json_output:
            # JSON keeps listing everything unless asked otherwise (scripts rely on it).
            capped = None if all_tasks else limit
            tasks = select_tasks(
                ctx.repo.list_all(), limit=capped, statuses=statuses, active=active
            )
            _emit_json(list_view(sorted(tasks, key=lambda t: t["id"])))
        else:
            capped = None if all_tasks else (limit or DEFAULT_LIST_LIMIT)
            typer.echo(
                format_task_list(ctx.repo, limit=capped, statuses=statuses, active=active)
            )
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
def events(
    since: int = typer.Option(0, "--since", help="Only events with an id greater than this."),
    task_id: int | None = typer.Option(None, "--task", help="Only this task's events."),
    limit: int = typer.Option(500, help="At most N events."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Follow events across all tasks, oldest first, from a cursor.

    Pass the returned cursor back as --since to get only what happened after it.
    """
    ctx = _ctx()
    feed = event_feed_view(ctx.log.since(since, task_id=task_id, limit=limit), since)
    if json_output:
        _emit_json(feed)
        return
    for e in feed["events"]:
        step = f" {e['step']}" if e["step"] else ""
        message = f"  {e['message']}" if e["message"] else ""
        typer.echo(f"#{e['id']} task {e['task_id']} {e['event_type']}{step}{message}")
    typer.echo(f"cursor: {feed['cursor']}", err=True)


@app.command()
def artifacts(
    task_id: int = typer.Argument(...),
    all_rows: bool = typer.Option(
        False, "--all", help="Every recorded row, not just the latest of each type."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show the artifacts a task's steps recorded (type, path, sha256)."""
    ctx = _ctx()
    if ctx.repo.get_task(task_id) is None:
        if json_output:
            _emit_json({"error": "not_found", "id": task_id})
        else:
            typer.echo(f"task {task_id} not found")
        raise typer.Exit(1)
    log = getattr(ctx.service("store"), "artifacts", None)
    if log is None:
        rows = []
    else:
        rows = log.list_for_task(task_id) if all_rows else log.latest(task_id)
    view = artifacts_view(rows)
    if json_output:
        _emit_json(view)
        return
    if not view:
        typer.echo(f"task {task_id} has no recorded artifacts")
    for a in view:
        typer.echo(f"{a['type']}  {a['path']}  sha256:{a['sha256'][:12]}  ({a['step']})")


items_app = typer.Typer(
    help="Work items of external steps: list, show, claim, release, submit.",
    no_args_is_help=False,
)
app.add_typer(items_app, name="items")


@items_app.callback(invoke_without_command=True)
def items(
    typer_ctx: typer.Context,
    all_items: bool = typer.Option(False, "--all", help="Include closed items."),
    task_id: int | None = typer.Option(None, "--task", help="Only this task's items."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List work items (default: open, claimed or submitted ones)."""
    if typer_ctx.invoked_subcommand is not None:
        return
    from runspool.persistence.work_items import ACTIVE

    ctx = _ctx()
    rows = [] if ctx.items is None else ctx.items.list_items(
        statuses=None if all_items else ACTIVE, task_id=task_id
    )
    view = [item_view(r) for r in rows]
    if json_output:
        _emit_json(view)
        return
    if not view:
        typer.echo("no work items" if all_items else "no open work items")
    for i in view:
        who = f"  by {i['claimed_by']}" if i["claimed_by"] and i["status"] == "claimed" else ""
        typer.echo(
            f"#{i['id']}  task {i['task_id']}  {i['step']}  {i['status']}  {i['kind']}  "
            f"attempt {i['attempt']}/{i['max_attempts']}{who}"
        )


def _item_guard(action):
    """Run a work-item action; a missing item or one in the wrong state is a clean
    error and a non-zero exit code."""
    try:
        return action()
    except KeyError as exc:
        typer.echo(f"not found: {exc.args[0] if exc.args else exc}")
        raise typer.Exit(1) from exc
    except (ValueError, IllegalTransition) as exc:
        typer.echo(str(exc))
        raise typer.Exit(1) from exc


@items_app.command("show")
def items_show(
    item_id: int = typer.Argument(...),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Show a work item: what it asks for, and what was submitted."""
    item = _item_guard(lambda: commands.get_item(_ctx(), item_id))
    view = item_view(item, full=True)
    if json_output:
        _emit_json(view)
        return
    typer.echo(f"Work item #{view['id']} (task {view['task_id']}, step {view['step']})")
    typer.echo(f"  Status:       {view['status']}")
    typer.echo(f"  Kind:         {view['kind']}")
    typer.echo(f"  Attempt:      {view['attempt']} of {view['max_attempts']}")
    if view["previous_error"]:
        typer.echo(f"  Last attempt: {view['previous_error']}")
    if view["error"]:
        typer.echo(f"  Rejected:     {view['error']}")
    if view["instructions"]:
        typer.echo(f"  Instructions: {view['instructions']}")
    if view["payload"]:
        typer.echo(f"  Payload:      {json.dumps(view['payload'], ensure_ascii=False)}")
    if view["submission"] is not None:
        typer.echo(f"  Submission:   {json.dumps(view['submission'], ensure_ascii=False)}")


@items_app.command("claim")
def items_claim(
    item_id: int = typer.Argument(...),
    by: str | None = typer.Option(None, "--by", help="Who claims it (default: this user)."),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """Claim an open work item. Prints the claim token that submit and release need."""
    who = by or _who()
    token = _item_guard(lambda: commands.claim_item(_ctx(), item_id, by=who))
    if json_output:
        _emit_json({"id": item_id, "claimed_by": who, "claim_token": token})
        return
    typer.echo(f"Claimed work item {item_id}; token: {token}")


@items_app.command("release")
def items_release(
    item_id: int = typer.Argument(...),
    token: str | None = typer.Option(None, "--token", help="The claim token."),
    force: bool = typer.Option(False, "--force", help="Release anyone's claim."),
) -> None:
    """Give a claimed work item back, so it can be claimed again."""
    if token is None and not force:
        typer.echo("give the claim --token, or --force to release anyone's claim")
        raise typer.Exit(1)
    _item_guard(lambda: commands.release_item(_ctx(), item_id, token=None if force else token))
    typer.echo(f"Released work item {item_id}")


@items_app.command("submit")
def items_submit(
    item_id: int = typer.Argument(...),
    token: str = typer.Option(..., "--token", help="The claim token."),
    file: Path | None = typer.Option(None, "--file", help="Submit this file (path + sha256)."),
    value: str | None = typer.Option(None, "--value", help="Submit this text."),
    json_value: str | None = typer.Option(
        None, "--json-value", help="Submit this JSON object as it is."
    ),
    by: str | None = typer.Option(None, "--by", help="Who submits it (default: this user)."),
) -> None:
    """Submit a result for a claimed work item. The step checks it when the task
    next runs (``runspool run`` or the daemon); see ``runspool items show``."""
    from runspool.engine.external import sha256_of

    given = [x is not None for x in (file, value, json_value)]
    if sum(given) != 1:
        typer.echo("give exactly one of --file, --value or --json-value")
        raise typer.Exit(1)
    if file is not None:
        # A file system error's message holds the path: report only its kind.
        try:
            try:
                path = file.expanduser().resolve()
            except RuntimeError as exc:  # a symlink loop, before Python 3.13
                typer.echo(f"cannot read the file to submit ({type(exc).__name__})")
                raise typer.Exit(1) from None
            if not path.is_file():
                typer.echo("not found: the file to submit")
                raise typer.Exit(1)
            submission: Any = {"path": str(path), "sha256": sha256_of(path)}
        except OSError as exc:
            typer.echo(f"cannot read the file to submit ({type(exc).__name__})")
            raise typer.Exit(1) from None
    elif value is not None:
        submission = {"value": value}
    else:
        try:
            submission = json.loads(json_value or "")
        except ValueError as exc:
            typer.echo(f"--json-value is not JSON: {exc}")
            raise typer.Exit(1) from exc
    who = by or _who()
    _item_guard(
        lambda: commands.submit_item(_ctx(), item_id, token=token, value=submission, by=who)
    )
    typer.echo(f"Submitted work item {item_id}; it is checked when the task next runs")


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
def plugins(
    installed: bool = typer.Option(
        False, "--installed", help="Also list installed plugins this profile does not mount."
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit JSON."),
) -> None:
    """List the profile's plugins with their state and declared metadata."""
    ctx = _ctx()
    infos = ctx.service("startup").loader.describe(installed=installed)
    if json_output:
        _emit_json({"plugins": [info.to_dict() for info in infos]})
        return
    for info in infos:
        name = info.id or f"({info.plugin})"
        category = f" [{info.meta.category}]" if info.meta and info.meta.category else ""
        line = f"{name:<24} {info.state:<11}{category}"
        if info.meta and info.meta.description:
            line += f"  {info.meta.description}"
        typer.echo(line.rstrip())
        if info.error:
            typer.echo(f"    {info.error}")


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
    """Report whether the daemon is running (exit 1 if its process is suspended)."""
    s = daemon_status(_ctx())
    if json_output:
        _emit_json(s)
    elif s.get("stopped"):
        group = _process_group(s["pid"])
        typer.echo(
            f"daemon process exists (pid={s['pid']}) but is STOPPED: it schedules nothing.\n"
            f"Resume it with `kill -CONT -{group}`, or stop it (`kill -TERM -{group}; "
            f"kill -CONT -{group}`) and start it again with stdin from /dev/null."
        )
    else:
        typer.echo(f"daemon running (pid={s['pid']})" if s["running"] else "daemon not running")
    if s.get("stopped"):
        raise typer.Exit(1)


def _process_group(pid: int) -> int:
    try:
        return os.getpgid(pid)
    except OSError:
        return pid


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
    # Take the executor lock before claiming the pid file: a `run` in progress
    # keeps the daemon from starting rather than racing it.
    lock = getattr(d, "lock", None)
    if lock is not None and not lock.acquire():
        _fail(lock.busy_error("cannot start the daemon"))
    pid_file = daemon_pid_file(ctx)
    write_pid(pid_file, os.getpid())
    _detach_stdin()

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


def _detach_stdin() -> None:
    """Point fd 0 at /dev/null: a resident daemon never reads the terminal.

    Started as a background job (``nohup runspool daemon &``; macOS nohup leaves stdin
    alone), the daemon would otherwise share the terminal as stdin with every tool its
    steps run. One that reads it (ffmpeg does by default) gets SIGTTIN, which stops the
    whole job: the daemon looks alive but schedules nothing. Children inherit fd 0.
    """
    try:
        devnull = os.open(os.devnull, os.O_RDONLY)
    except OSError:
        return
    try:
        os.dup2(devnull, 0)
    finally:
        os.close(devnull)


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


# Built-in commands added after plugins could already register commands of their
# own. A plugin command of the same name predates them, so it wins: the built-in
# yields instead of breaking that plugin.
_YIELDING_BUILTINS = frozenset({"artifacts", "items"})


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
        if contributed.name in _YIELDING_BUILTINS:
            combined.registered_commands = [
                c for c in combined.registered_commands if _command_name(c) != contributed.name
            ]
            combined.registered_groups = [
                g for g in combined.registered_groups if g.name != contributed.name
            ]
        elif contributed.name in builtin:
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


def _command_name(command: Any) -> str:
    return command.name or command.callback.__name__.replace("_", "-")


def _builtin_names() -> set[str]:
    commands_ = {_command_name(c) for c in app.registered_commands}
    return commands_ | {g.name for g in app.registered_groups if g.name}


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
