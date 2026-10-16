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
- `-w/--workflow NAME` — workflow to use (default: the profile's `default_workflow`,
  else `local_file`).
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
- `--force` — skip the check for a live daemon's pid file. The executor lock
  still applies: `run` refuses to start while any other process (a daemon,
  another `run`) is executing tasks on the same database, because it starts by
  recovering interrupted tasks and would steal that process's in-flight work.

One-shot; ideal for demos, batch processing, and cron. Tasks waiting on a
deferral delay or a timed retry stay queued for a later `run` or the daemon.
`run` refuses to start if an enabled plugin is not active or a configured step
cannot be imported; `runspool doctor` shows why.

### `runspool daemon`

Run a resident loop in the foreground (Ctrl-C or SIGTERM to stop). Use for
long-running or deferred work. It refuses to start if a daemon is already
running, or for the same plugin reasons as `run`. Companion commands:

- `runspool daemon-status [--json]` — is a daemon running, and its PID. If its
  process exists but is suspended (state `T`: SIGSTOP, or SIGTTIN when a background
  job touched the terminal) it says so and exits 1: such a daemon holds its tasks
  and schedules nothing. JSON: `{"running", "pid", "stopped"}`.
- `runspool daemon-stop` — signal a running daemon to stop.

## Observing

### `runspool status [<id>]`

With no id, list tasks, newest first: the latest 20 by default. A footer says how
many tasks are not shown and names unfinished ones hidden by the limit, so an old
task waiting on you is never out of sight. With an id, show details: status, the
step timeline (with each run's note), recent events, and last error.

- `-n/--limit N` — the newest N tasks.
- `--all` — every task.
- `--active` — only unfinished tasks (anything not completed, partially completed
  or terminated).
- `--status NAME` — only tasks in that status; repeatable.

```bash
runspool status                          # latest 20
runspool status -n 50
runspool status --active
runspool status --status manual_required
runspool status 1
runspool status --json                   # every task (JSON is not capped by default)
runspool status --json --active          # filters apply to JSON too
runspool status 1 --json                 # one task + events + step_runs
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

### `runspool events --since <cursor>`

Events of every task with an id above `cursor`, oldest first, to follow
progress from another process (see
[agent-json-output.md](agent-json-output.md)).

```bash
runspool events --since 0 --json            # {"events": [...], "cursor": M}
runspool events --since 42 --task 1 --limit 100 --json
```

`cursor` in the output is the last id returned, or the given one when nothing
is new: pass it as `--since` next time. If a plugin registers its own `events`
command, the plugin's command is used instead: plugins could register that
name before RunSpool had this command.

### `runspool artifacts <id>`

The artifacts a task's steps recorded (see
[Artifact contracts](writing-steps.md#artifact-contracts)): the latest of each
type, oldest first.

```bash
runspool artifacts 1
runspool artifacts 1 --all --json   # every row, including earlier runs
```

`--json` gives an array of `{id, type, path, sha256, size_bytes, step,
step_run_id, created_at}`; a task with none gives `[]`, an unknown id
`{"error": "not_found", ...}` and exit code 1.

If a plugin registers its own `artifacts` command, the plugin's command is used
instead: plugins could register that name before RunSpool had this command.

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
a running, awaiting-approval, awaiting-external or finished task. Moving a task
clears any approval it held and cancels its active work item.

`approve` and `reject` record who decided (`cli:<your user name>`) in the
task's `approval_decided` event; `--reason` is kept there and in `last_error`.

```bash
runspool approve 3
runspool reject 3 --reason "wrong cover image"
runspool wake 7
```

## External work items

Work items are opened by [external steps](writing-steps.md#external-steps): work
done outside the engine, by an agent or a person.

```bash
runspool items                      # open, claimed or submitted items
runspool items --all --task 3 --json
runspool items show 5 [--json]      # instructions, payload, previous_error, submission
runspool items claim 5 [--by NAME] [--json]   # prints the claim token
runspool items submit 5 --token T --file result.md   # or --value TEXT / --json-value JSON
runspool items release 5 --token T  # give it back (--force: anyone's claim)
```

`items --json` gives an array of `{id, task_id, step, status, kind, attempt,
max_attempts, previous_error, error, claimed_by, claimed_at, submitted_by,
submitted_at, created_at, updated_at}`; `items show --json` adds `instructions`,
`payload`, `submission` and `step_run_id`. The claim token is only printed by
`claim`. Status: `open`, `claimed`, `submitted`, then `accepted`, `rejected`,
`exhausted` (rejected with no attempts left) or `cancelled`.

`submit` only records the result and queues the task; the step checks it when
the task next runs (`runspool run` or the daemon). Claiming an item someone else
holds, submitting without the right token, or acting on a closed item fails with
the reason and exit code 1. If a plugin registers its own `items` command, the
plugin's command is used instead.

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
permissions. Once a step declares an artifact contract, an `artifacts` check
reports whether every declared requirement is provided by an earlier step. Plugins add their own checks — e.g. whether the credentials they
need are configured (names and sources only, never values).

```bash
runspool doctor
runspool doctor --json        # array of {name, ok, detail}
```

### `runspool plugins`

List the profile's plugin entries: state (`active`, `pending`, `failed`,
`skipped`, ...), and what each declares about itself — category, description,
external dependencies, hosts and its config model's JSON Schema (see
[plugins.md](plugins.md#describing-a-plugin)).

- `--installed` — also list installed plugins (entry points) the profile does not
  mount: `id` is `null` and `state` is `available`, or `unavailable` with an
  `error` when one cannot be loaded here. They are imported to read their
  metadata, never applied.

```bash
runspool plugins
runspool plugins --installed --json   # {"plugins": [{id, plugin, state, error, category,
                                      #   description, external, hosts, config_schema,
                                      #   distribution, version}]}
```

If a plugin registers its own `plugins` command, the plugin's command is used
instead: plugins could register that name before RunSpool had this command.

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
  warning and the plugin's command is ignored. The exception is the built-in
  commands added in 0.3.0, `artifacts`, `items`, `events` and `plugins`:
  plugins could register these names before RunSpool had them, so a plugin's
  command with one of these names is used instead, and `--help` lists it with
  the plugin's help.
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
