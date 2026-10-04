# Workflows and configuration

A workflow is an ordered list of step names. Configuration is a single YAML file
(default `config.yaml`, any name with `-c`), validated on load. Since 0.2 that
file is a **profile**: besides the engine settings it says which plugins to
mount. A 0.1 config file is a valid profile and behaves as before.

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
workspace_root: ./workspace          # base directory for all state (required);
                                     # relative paths resolve against this file's directory

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

first_task_id: 1000                  # number new tasks from here (never lowers it)
default_workflow: local_file         # what `runspool add` uses without --workflow

workflows:
  local_file:
    steps: [ingest_file, classify_text, normalize_markdown, summarize_text, archive]

# Custom steps loaded from your own code (see writing-steps.md).
plugin_paths: [steps]                # dirs added to sys.path, relative to this file
steps:
  my_step:
    import: "my_module:MyStep"       # "<module path>:<Step subclass>"

# Plugins (see "The config file is a profile" below).
bundles: [core, builtin-steps]       # the default when omitted
required: []                         # entries that must be active, or startup fails
allow: []                            # exact "package@version" compatibility exemptions
patch: []                            # insert / modify / disable plugin entries
```

## How fields are used

- **workspace_root** — everything Runspool writes lives here: the database, logs,
  per-task working directories (`tasks/<id>/`), and archived output
  (`ready/<id>/`). A relative path is resolved against the config file's
  directory, so the same profile means the same workspace whichever directory a
  command or the daemon starts in.
- **default_workflow** — the workflow `runspool add` uses when `--workflow` is not
  given (default `local_file`). It may name a workflow a plugin contributes;
  `runspool doctor` reports it if no such workflow is defined.
- **scheduler.max_retries** — the retry budget stamped onto each new task. Set it
  to `0` to make the first failure terminal (it becomes `manual_required`
  immediately) — useful when failures mean "bad input", not "transient glitch".
- **scheduler.retry_delay_seconds** — delay before a failed task is retried.
  `0` (default) retries on the next tick, so one `runspool run` consumes the
  whole budget; a positive value is a backoff whose timed retries are driven by
  the `daemon`. Retries are automatic either way (see
  [concepts.md](concepts.md#retries)).
- **worker_pool.size** — the number of worker threads.
- **worker_pool.heartbeat_timeout_seconds** — how long a running task's heartbeat
  may go stale before the daemon reclaims it.
- **concurrency** — caps how many tasks may run a given step at once. Keep it at
  `1` for steps that must not overlap; raise it for cheap, parallel-safe steps.
- **first_task_id** — make new task ids start at this number (it never lowers the
  numbering). For a database that continues an older system whose task ids are
  used as keys elsewhere.
- **plugin_paths** / **steps** — custom steps loaded from modules next to your
  config; see [writing-steps.md](writing-steps.md#registering-a-step-from-config).

Every key that is not a profile key (`bundles`, `required`, `allow`, `patch`) is
an engine setting. An unknown setting is ignored with a warning, which
`runspool doctor` reports under `profile`.

## The config file is a profile

Runspool is assembled from plugins (see [concepts.md](concepts.md#plugins)). The
profile decides which, through four keys:

- **`bundles`** — named sets of plugin entries, applied in the order listed.
  Runspool ships `core` (the engine: store, steps, workflows, tasks, runtime,
  doctor, credentials, approval, CLI extensions) and `builtin-steps` (one entry
  per built-in step, ids `builtin-ingest_file`, …, `builtin-archive`). Installed
  packages can provide more, e.g. `wechat` from
  [runspool-wechat](../plugins/runspool-wechat/). Omit `bundles` to get
  `[core, builtin-steps]`; if you list bundles, list `core` too — its core
  entries cannot be left out or disabled.
- **`required`** — entry ids that must end up active. If one is not (missing
  package, bad config, failed import), every command fails at startup with the
  reason, instead of running without it.
- **`allow`** — exact `package@version` exemptions from the version check: a
  plugin package that declares an incompatible `runspool` range is skipped
  unless allowed here. (A compatibility check, not a security boundary.)
- **`patch`** — your own changes to the entry list, applied after the bundles:

```yaml
workspace_root: ./workspace
bundles: [core, builtin-steps, wechat]
required: [wechat]
patch:
  # add an entry: an installed plugin by its entry-point name, or "module:attr"
  - insert:
      - {id: creator, plugin: example-creator}
      - {id: my-steps, plugin: "my_plugins.steps:plugin", config: {lang: en}}
  # turn an entry off (there is no "remove")
  - id: builtin-archive
    disabled: true
  # configure an entry: config is deep-merged into what the bundle set
  - id: wechat
    config:
      author: Your Name
      theme: {strong: "font-weight:700;color:#0f766e;"}
```

Patch rules:

- `insert` adds entries (`id`, `plugin`, optional `config` and `disabled`). An
  `id` that already exists is an error.
- A patch with an `id` modifies that entry. `config` is **deep-merged**: mappings
  merge key by key, anything else (lists included) replaces the old value.
  `disabled` replaces the flag. `plugin`, if given, must match the entry's plugin
  (an identity check; on mismatch the patch is skipped with a warning).
- A patch for an id that does not exist is skipped with a warning, and
  `config: null` (often an empty YAML value by mistake) is ignored with a
  warning. `runspool doctor` lists these warnings.
- Each plugin validates its own `config`; an invalid one fails that plugin with
  a message naming the fields (never echoing the values). Secrets never go in
  config — only their names (see [concepts.md](concepts.md#credentials)).

A plugin that fails to load, or is skipped, does not stop read-only commands, but
`run` and `daemon` refuse to start until you fix it or disable it in `patch`.
`runspool doctor` says which entry failed and why.

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

## Workflows from plugins

A plugin can contribute a default workflow — `runspool-wechat` adds
`wechat_article` (`wechat_render → wechat_draft`), the example creator plugin
adds `creator_publishing`. They are listed by `runspool workflows` like your own.
If your profile defines a workflow with the same name, **the profile wins**: that
is how you reorder or extend a plugin's workflow without forking it.

## Mixing built-in and custom steps

A workflow can freely interleave built-in steps (like `archive`), steps loaded
from config, and steps from plugins. The example workflows `client_intel` and
`creator_publishing` do exactly this — several custom steps followed by the
built-in `archive`.

## Validation

`runspool doctor` checks that every step named in every workflow resolves in the
registry and every configured step imports, and that every enabled plugin is
active. A typo, an unregistered step, a broken import or a plugin that failed to
load is reported before you run anything.
