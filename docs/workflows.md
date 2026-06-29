# Workflows and configuration

A workflow is an ordered list of step names. Configuration is a single YAML file
(default `config.yaml`), validated on load.

## Minimal config

```yaml
workspace_root: ./workspace

workflows:
  local_file:
    steps: [ingest_file, classify_text, normalize_markdown, summarize_text, archive]
```

If you omit `workflows`, Runspool provides the `local_file` workflow above so a
fresh `runspool init` is immediately runnable.

## Full config

```yaml
workspace_root: ./workspace          # base directory for all state (required)

# Optional path overrides (relative paths resolve under workspace_root).
database_path: ./workspace/runspool.db
logs_dir: ./workspace/logs
runtime_dir: ./workspace/runtime

scheduler:
  poll_interval_seconds: 5           # daemon tick interval
  max_retries: 3                     # default retry budget for new tasks
  retry_delay_seconds: 60

worker_pool:
  size: 4                            # max concurrent step executions
  heartbeat_timeout_seconds: 1800    # reclaim a task whose worker went silent

concurrency:                         # per-step quota (default 1)
  ingest_file: 4

workflows:
  local_file:
    steps: [ingest_file, classify_text, normalize_markdown, summarize_text, archive]

# Custom step plugins (see writing-steps.md).
plugin_paths: [steps]                # dirs added to sys.path, relative to this file
steps:
  my_step:
    import: "my_module:MyStep"       # "<module path>:<Step subclass>"
```

## How fields are used

- **workspace_root** — everything Runspool writes lives here: the database, logs,
  per-task working directories (`tasks/<id>/`), and archived output
  (`ready/<id>/`).
- **scheduler.max_retries** — the retry budget stamped onto each new task. Set it
  to `0` to make the first failure terminal (it becomes `manual_required`
  immediately) — useful when failures mean "bad input", not "transient glitch".
- **scheduler.retry_delay_seconds** — delay before a failed task is retried.
  `0` (default) retries on the next tick, so one `runspool run` consumes the
  whole budget; a positive value is a backoff whose timed retries are driven by
  the `daemon`. Retries are automatic either way (see
  [concepts.md](concepts.md#retries)).
- **worker_pool.size** — the number of worker threads.
- **concurrency** — caps how many tasks may run a given step at once. Keep it at
  `1` for steps that must not overlap; raise it for cheap, parallel-safe steps.

## Multiple workflows

Define as many as you like and pick one per task with `--workflow`:

```yaml
workflows:
  local_file:
    steps: [ingest_file, classify_text, normalize_markdown, summarize_text, archive]
  intake_only:
    steps: [ingest_file, archive]
```

```bash
runspool add ./a.txt --workflow local_file
runspool add ./b.txt --workflow intake_only
```

## Mixing built-in and custom steps

A workflow can freely interleave built-in steps (like `archive`) with your own
plugin steps. The two example workflows `client_intel` and `creator_publishing`
do exactly this — several custom steps followed by the built-in `archive`.

## Validation

`runspool doctor` checks that every step named in every workflow resolves in the
registry. A typo, an unregistered step, or a broken plugin import is reported
before you run anything.
