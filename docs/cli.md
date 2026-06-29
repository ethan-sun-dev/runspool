# CLI reference

Every command accepts a global `-c/--config-path` option (default
`config.yaml`):

```bash
runspool -c path/to/config.yaml <command> ...
```

Read commands accept `--json` for machine-readable output.

## Setup

### `runspool init`

Create a config file (if absent) and initialise the database.

```bash
runspool init --workspace-root ./workspace
```

- `--workspace-root PATH` — base directory for all state (default `./workspace`).
- Does not overwrite an existing config.

## Creating and advancing tasks

### `runspool add <input>`

Queue a task.

```bash
runspool add ./invoice.txt --workflow local_file --name "June invoice"
```

- `<input>` — the task input (e.g. a file or directory path).
- `-w/--workflow NAME` — workflow to use (default `local_file`).
- `--name NAME` — human-readable label (defaults to a value derived by the first
  step, e.g. the file stem).
- `--force` — allow a second active task for the same input (otherwise blocked).

### `runspool run`

Advance every runnable task until no further progress is made, then exit.

```bash
runspool run
runspool run --json     # {"rounds": N, "tasks": [...]}
```

One-shot; ideal for demos, batch processing, and cron.

### `runspool daemon`

Run a resident loop in the foreground (Ctrl-C to stop). Use for long-running or
deferred work. Companion commands:

- `runspool daemon-status [--json]` — is a daemon running, and its PID.
- `runspool daemon-stop` — signal a running daemon to stop.

## Observing

### `runspool status [<id>]`

With no id, list all tasks. With an id, show details: status, step timeline,
recent events, and last error.

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

The event history for a task.

```bash
runspool logs 1 --limit 20
runspool logs 1 --json
```

### `runspool overview`

Counts by status.

```bash
runspool overview
runspool overview --json      # {"queued": 2, "completed": 5, ...}
```

## Controlling

| Command | Effect |
| --- | --- |
| `runspool pause <id>` | Request a pause (applies after the current step). |
| `runspool resume <id>` | Return a paused task to the queue. |
| `runspool retry <id>` | Requeue a failed / manual_required task from its step. |
| `runspool terminate <id>` | Stop a task permanently. |
| `runspool set-priority <id> <n>` | Set scheduling priority (higher runs first). |
| `runspool set-retries <id> <n>` | Set the retry ceiling and reset the count. |
| `runspool set-step <id> <step>` | Move a task to a specific step in its workflow. |

## Introspection

### `runspool workflows`

List workflows and their steps.

```bash
runspool workflows
runspool workflows --json     # {"local_file": ["ingest_file", ...]}
```

### `runspool doctor`

Check the local environment: Python version, a writable workspace, a reachable
database, at least one workflow, and that every step referenced by a workflow
resolves (built-in or plugin — this catches typos and broken plugin imports).

```bash
runspool doctor
runspool doctor --json
```

## Exit codes

`0` on success. Commands exit non-zero on a missing task, an unknown workflow,
an invalid argument, or a blocked duplicate `add`. The error message is printed
on stderr/stdout; with `--json`, errors are returned as a JSON object where
applicable.
