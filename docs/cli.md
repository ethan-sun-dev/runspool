# CLI reference

Every command accepts a global `-c/--config-path` option (default
`config.yaml` in the current directory). It goes **before** the command, in any
of the usual forms:

```bash
runspool -c path/to/config.yaml <command> ...
runspool -cpath/to/config.yaml <command> ...
runspool --config-path path/to/config.yaml <command> ...
runspool --config-path=path/to/config.yaml <command> ...
```

The config file is a *profile*: besides engine settings it can mount plugins,
and plugins can add commands of their own (see
[Plugin commands](#plugin-commands)). Read commands accept `--json` for
machine-readable output.

## Setup

### `runspool init`

Create a config file (if absent) and initialise the database.

```bash
runspool init --workspace-root ./workspace
```

- `--workspace-root PATH` — base directory for all state (default `./workspace`).
  A relative path resolves against the config file's directory.
- Does not overwrite an existing config (its `workspace_root` is then used).
- Opening an existing database applies any pending schema migrations, so a 0.1
  database is upgraded in place.

## Creating and advancing tasks

### `runspool add <input>`

Queue a task.

```bash
runspool add ./invoice.txt --workflow local_file --name "June invoice"
runspool add ./notes.md --meta source=mail --meta lang=en
runspool add ./chapter-2 --parent 1 --workflow translate
```

- `<input>` — the task input (e.g. a file or directory path). An input that
  names an existing file or directory is stored as an absolute path.
- `-w/--workflow NAME` — workflow to use (default `local_file`).
- `--name NAME` — human-readable label (defaults to a value derived by the first
  step, e.g. the file stem).
- `--meta KEY=VALUE` — task metadata, repeatable. Values are strings; steps read
  them from `ctx.task["metadata"]`.
- `--parent ID` — the task this one derives from (recorded as
  `parent_task_id`; the parent must exist).
- `--force` — allow a second active task for the same input (otherwise blocked).

### `runspool run`

Advance every runnable task until no further progress is made, then exit.

```bash
runspool run
runspool run --json     # {"rounds": N, "tasks": [...]}
```

- `--json` — print a JSON summary instead of progress lines.
- `--force` — run even though a daemon is live. `run` starts by recovering
  interrupted tasks, which would steal the daemon's in-flight work; use this only
  when you know the daemon is not executing anything.

One-shot; ideal for demos, batch processing, and cron. Tasks waiting on a
deferral delay or a timed retry stay queued for a later `run` or the daemon.
`run` refuses to start if an enabled plugin is not active or a configured step
cannot be imported; `runspool doctor` shows why.

### `runspool daemon`

Run a resident loop in the foreground (Ctrl-C or SIGTERM to stop). Use for
long-running or deferred work. It refuses to start if a daemon is already
running, or for the same plugin reasons as `run`. Companion commands:

- `runspool daemon-status [--json]` — is a daemon running, and its PID.
- `runspool daemon-stop` — signal a running daemon to stop.

## Observing

### `runspool status [<id>]`

With no id, list all tasks. With an id, show details: status, the step timeline
(with each run's note), recent events, and last error.

```bash
runspool status
runspool status 1
runspool status --json        # list of task objects
runspool status 1 --json      # one task + events + step_runs
```

### `runspool inspect <id>`

An agent-friendly snapshot: current state, artifacts, valid actions, and a
suggested next action. See [agent-json-output.md](agent-json-output.md).

```bash
runspool inspect 1
runspool inspect 1 --json
```

### `runspool logs <id>`

The event history for a task, newest first.

```bash
runspool logs 1 --limit 20
runspool logs 1 --json
```

- `--limit N` — most recent N events (default 20).

### `runspool overview`

Counts by status.

```bash
runspool overview
runspool overview --json      # {"queued": 2, "completed": 5, ...}
```

## Controlling

| Command | Effect |
| --- | --- |
| `runspool pause <id>` | Pause a queued task now, or a running one at the end of its current step. |
| `runspool resume <id>` | Return a paused task to the queue. |
| `runspool retry <id>` | Requeue a failed / manual_required task from its current step. |
| `runspool terminate <id>` | Stop a task permanently (a running step finishes first). |
| `runspool wake <id>` | Make a task that is waiting on a deferral delay runnable now. |
| `runspool approve <id>` | Approve the side-effect step a task is waiting on (that one attempt) and requeue it. |
| `runspool reject <id> [--reason TEXT]` | Refuse it; the task goes to `manual_required` (`retry` asks again). |
| `runspool set-priority <id> <n>` | Set scheduling priority (higher runs first). |
| `runspool set-retries <id> <n>` | Set the retry ceiling and reset the count. |
| `runspool set-step <id> <step> [--force]` | Move a task to a specific step in its workflow. |

Each is checked by the state machine; an action not valid for the task's current
status is refused with the reason and a non-zero exit (the allowed states are in
[concepts.md](concepts.md#state-machine)). `set-step` works on `failed` and
`manual_required` tasks; `--force` also allows `queued` and `paused`, but never
a running, awaiting-approval or finished task. Moving a task clears any approval
it held.

`approve` and `reject` record who decided (`cli:<your user name>`) in the
task's `approval_decided` event; `--reason` is kept there and in `last_error`.

```bash
runspool approve 3
runspool reject 3 --reason "wrong cover image"
runspool wake 7
```

## Introspection

### `runspool workflows`

List workflows and their steps, including default workflows contributed by
plugins.

```bash
runspool workflows
runspool workflows --json     # {"local_file": ["ingest_file", ...]}
```

### `runspool doctor`

Check the local environment: Python version, a writable workspace, a reachable
database, at least one workflow, that every step referenced by a workflow
resolves and every configured step imports (this catches typos and broken
imports), that every enabled plugin is active (and why not), profile warnings
(unknown settings, patches that matched nothing), and the credentials file's
permissions. Plugins add their own checks — e.g. whether the credentials they
need are configured (names and sources only, never values).

```bash
runspool doctor
runspool doctor --json        # array of {name, ok, detail}
```

## Plugin commands

Plugins can contribute commands or command groups. They appear in
`runspool --help` next to the built-in ones when the profile that mounts the
plugin is in effect, so pass the profile with `-c` (or keep it as
`config.yaml` in the current directory):

```bash
runspool -c runspool.yaml --help              # built-ins + e.g. "wechat"
runspool -c runspool.yaml wechat --help
runspool -c runspool.yaml wechat preview post/wechat.md
```

- A plugin command cannot replace a built-in one; a clash is reported as a
  warning and the plugin's command is ignored.
- If the profile itself does not load (malformed, a `required` plugin failed, a
  core entry disabled), the built-in commands are still offered and report the
  error; invoking a plugin command prints why it is unavailable.
- If only that plugin failed (say, its config is invalid), its commands are
  absent; `runspool -c ... doctor` names the failure.

The official [runspool-wechat](../plugins/runspool-wechat/) plugin, for example,
adds `runspool wechat token` and `runspool wechat preview <article.md>`. Writing
your own: [plugins.md](plugins.md).

## Exit codes

`0` on success. Commands exit non-zero on a missing task, an unknown workflow or
parent task, an invalid argument, an action not allowed in the task's current
status, a blocked duplicate `add`, a profile that does not load, or (for `run`
and `daemon`) a plugin or step that is not available. The error message is
printed on stderr/stdout; with `--json`, a missing task is returned as
`{"error": "not_found", "id": N}`.
