# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Fixed
- `add_task` no longer fails when the task input is long text: checking whether
  the input is an existing file raised `OSError` (File name too long) for text
  longer than the platform's path limit, so the task was never created. An input
  that cannot be a path is now kept as it is. (#26)

## [0.3.0] - 2026-10-16

<!-- Release date: set when 0.3.0 is published. -->

Upgrading from 0.2.x: read "Breaking" below and
[docs/upgrading.md](docs/upgrading.md). The official plugin
`runspool-wechat` 0.2.1 accepts RunSpool 0.3 (0.2.0 required `<0.3`).

### Added
- External steps, all optional (`runspool.engine.external`): a step that
  subclasses `ExternalStep` is done outside the engine (by an AI agent, by a
  person). When a task reaches it, the engine opens a work item from the step's
  `request(ctx)` (`WorkRequest(kind, instructions, payload)`) and the task waits
  in the new status `awaiting_external`. A client claims the item (one claimant
  wins), then submits a JSON object; that only records it and queues the task.
  The executor (`run` or the daemon, after the usual start-up checks) passes it
  to the step's `validate(ctx, submission)`: a returned `StepResult` is accepted
  and the workflow advances (updates and artifacts apply as for any step);
  `SubmissionRejected(reason)` uses up the attempt and opens the next one with
  the reason as `previous_error`, until `max_attempts` (default 3) is reached
  and the task goes to `manual_required` (`retry` starts over). Any other error
  is a step failure, and the submission is validated again on retry.
  `Submission.file()` checks a submitted path and its sha256 (a file that
  cannot be read rejects the submission naming only the kind of error, never
  the path). A task that ends in any terminal state (for instance a last
  external step skipped by its `when`), is terminated, or moves to another step
  cancels its remaining active item. Steps that are not external are
  unaffected. Known limits: claims do not expire, a submission is validated
  the next time its task can be scheduled (queueing, quotas and approval can
  delay that), and submissions have no size limit (see writing-steps.md).
  `items submit --file` reports a file it cannot read by the kind of error
  only, never its path.
- `runspool items [--all] [--task ID] [--json]`, `items show <id>`, `items claim
  <id>`, `items release <id>`, `items submit <id> --token T (--file PATH | --value
  TEXT | --json-value JSON)`.
- Child tasks from a step result (`runspool.engine.children`): a step may return
  `StepResult(spawn=[ChildTask(input, workflow, name=None, metadata=None,
  key=None)])`. When the step succeeds, the children are created in the same
  transaction that advances the parent (with a `created` event each, and a
  `children_spawned` event on the parent), so a crash leaves both or neither and
  a re-run never creates duplicates. Children are independent tasks
  (`parent_task_id` set, queued on their workflow's first step); the parent does
  not wait for them. A `key` names a child within its parent: running the step
  again (after `set-step`) does not create that child a second time. Nothing is
  spawned when the step fails or defers, or when its boundary terminates the
  task; a spawn that does not validate (not a list or tuple, even if empty or
  falsy; an unknown workflow; a key used twice; metadata that cannot be stored
  as a JSON object with sorted keys; an input, workflow name, first step,
  name, key or metadata that is not valid UTF-8) fails the
  step before any of its result is written, and creates nothing. A spawning
  step's `updates` and the end of its step run are written in the same
  transaction as its children and the parent's transition, so a failed or
  interrupted attempt leaves no update behind. An
  accepted external step submission may spawn children too.
- Artifact contracts, all optional: a step may declare the artifact types it
  `requires` and `provides` (`master`, `variant:blog`, or with a placeholder
  `variant:<target>`), and a workflow the types its input provides (`inputs`).
  `doctor` checks every workflow and gains an `artifacts` check, shown only
  when something declares a contract (malformed declarations included). `run`
  and the daemon check a task's workflow before each of its steps and refuse
  the step (`manual_required`, with the reason) while a declared requirement is
  not provided by an earlier step; other workflows keep running. A workflow
  mixing declared and undeclared steps may set `allow_unverified` to accept
  gaps only an undeclared step could fill (reported as unverified). See
  `runspool.engine.artifacts`.
- `StepResult.artifacts`: a step may record what it produced as
  `Artifact(type, path, sha256=None)`; the runner hashes files when no sha256 is
  given and stores type, path, sha256 and size in a new `artifacts` table, tied
  to the step run. `runspool artifacts <id> [--all] [--json]` lists them.
  If a plugin registers its own `artifacts` command, it is used instead (see
  "Breaking").
- `runspool events --since <cursor> [--task <id>] --json` and `EventLog.since()`:
  follow events across all tasks from a cursor. Event ids only increase, so a
  client in another process can resume from the last id it saw. `logs --json`
  output is unchanged.
- Executor lock: only one process executes steps on a database at a time. `run`,
  `run_until_idle()` and the daemon take an OS file lock next to the database
  (`runspool.db.executor-lock`) and refuse to start while another process holds
  it (`ExecutorBusy`, naming the holder's pid). The OS releases it when the
  holder exits, so there is no stale lock. See `runspool.executor_lock`.
- Plugin metadata, all optional: `category`, `description`, `external` (what a
  plugin needs outside Python, as `External(kind, name, hint)`) and `hosts`
  (free-form environment labels), on `Plugin(...)` or as attributes.
  `runspool.kernel.config_schema()` gives a config model's JSON Schema.
- `runspool plugins [--installed] [--json]`: each entry's state and metadata,
  with its config schema; `--installed` adds installed plugins the profile does
  not mount (imported, never applied). `Loader.describe()` is the API behind it.
- `doctor` has an `external` check: every `binary` an active plugin declares
  must be on `PATH`.
- macOS keychain credentials: `credentials-local` takes `keychain:
  {service, timeout_seconds}` to look a name up first as a generic password
  item (service = `service`, account = the name), read with
  `/usr/bin/security find-generic-password ... -w` by absolute path. Only names
  are arguments; the value comes back on a pipe, and `security` gets a fixed
  minimal environment (`PATH` only), never RunSpool's own. A missing item
  (exit 44) is absent; anything else (not macOS, no `security`, access denied,
  a timeout, non-UTF-8 output) raises `KeychainError` with no value, no
  `security` output, no chained exception and no local variable holding the
  output in its traceback, and no later source is asked. `doctor` gains a
  `credentials keychain` check when it is configured. See
  `runspool.core.keychain`.
- `credentials-local` takes `environment: false` to stop reading the process
  environment (with or without the keychain). Both settings are off by
  default: without them, credentials resolve exactly as before.
- Kernel: `Kernel.guard_changes(guard)` lets an application refuse changes to
  its plugins (load, reload, unload, provide or withdraw a service) once started;
  `Fiber.check_change(what)` applies the same guard to a service's own
  registrations (on the fiber, so no name is added to `Context`, whose
  attributes are service names); `Fiber.effect(..., change=...)` marks a
  registration whose early dispose is checked too. Shutting the kernel down is
  never refused.
- `runspool --version` prints the installed version.
- Schema versions 4, 5 and 6, each adding one table and leaving existing
  tables and rows untouched: `artifacts` (v4), `work_items` (v5) and
  `task_spawns` (v6, which step run of a parent spawned which child, and its
  key). A database from 0.2.x (v3) or 0.1.x is upgraded in place the first
  time 0.3.0 opens it; a 0.1.x database first gets the columns that versions
  1–3 add to its existing tables (see "Breaking" and
  [docs/upgrading.md](docs/upgrading.md)).

### Changed
- Contracts are checked against the steps registered when the runtime started
  (see "Breaking": the plugin set is fixed from then on).
- `run --force` now only skips the daemon pid-file check; it no longer lets
  `run` execute alongside another executor.
- `doctor` lists more checks: `external` always; `artifacts`, `plugin
  changes` and `credentials keychain` only when something uses them. Scripts
  that compare doctor's list of checks should expect them.
- Plugins in this repository accept RunSpool 0.3: `runspool-wechat` 0.2.1 and
  the example plugin declare `runspool>=0.2,<0.4`.

### Breaking
- **Databases cannot go back to 0.2.x.** 0.3.0 upgrades an older database
  (0.2.x writes schema v3; a 0.1.x database has no version and is upgraded
  from the v1 layout) to v6 in place, automatically, the first time it opens
  it. From then on 0.2.x refuses to open it
  (`SchemaTooNew`, for any version 4 or later). There is no downgrade: back
  the database up before upgrading if you may need to go back, and never point
  a 0.2.x installation at a database 0.3.0 has opened.
- **Upgrading waits for the executor lock.** Upgrading the schema of an
  existing database needs the executor lock:
  opening an older database while another process executes tasks raises
  `ExecutorBusy` instead of changing the schema under it. Up-to-date databases
  open as before.
- **One executor per database.** `run`, `run_until_idle()` and the daemon take
  the executor lock; a second one on the same database exits with
  `ExecutorBusy` (naming the holder's pid) instead of running alongside.
  Running two `run` processes on one database at once worked before; it no
  longer does. `run --force` does not get around it.
- **The plugin set is fixed once the runtime starts** (`run`, the daemon), with or
  without artifact contracts: every later plugin change (reloading a plugin with
  `fiber.restart()`, loading or unloading one, providing or withdrawing a
  service, registering or unregistering a step) is refused with
  `PluginChangeRefused` before anything changes. Restart the runtime or the
  daemon for plugin changes to take effect. `doctor` reports a `plugin changes`
  check once such a change was refused in its process. Code embedding RunSpool
  may still load plugins into a booted kernel before the runtime starts, but
  the runtime cannot start while plugins are still loading or unloading (from
  inside a plugin's `apply` or a disposer): it raises `StartupError` and
  starts and fixes nothing. Every way to build what executes steps
  (`build_coordinator`, `build_daemon`, `run_until_idle`) goes through this
  same check (`ensure_ready`) first. This withdraws run-time plugin reloading, which 0.2 already did not support (the
  plugin set changed only at startup and shutdown): what a reloaded plugin
  would declare is only known once its old version is gone, so a reload cannot
  be checked in advance.
- **`build_coordinator()` checks like `run`.** `RuntimeService.build_coordinator()`
  now starts with the same check as
  `build_daemon()` and `run_until_idle()` (`ensure_ready`): it raises
  `StartupError` when an enabled plugin failed to load or a lazily registered
  step does not import (it built a coordinator anyway before), and when
  plugins are still loading or unloading. It also fixes the plugin set, so
  plugin changes after it are refused.
- **Rules once artifact contracts are used.** Nothing changes for workflows
  that declare nothing. In a workflow where any step (or the workflow's
  `inputs`) declares a contract, every `requires` must be provided by an
  earlier step or by `inputs`; a step that declares nothing provides nothing,
  so a gap only it could fill is an error unless the workflow sets
  `allow_unverified`. `run` and the daemon refuse such a step
  (`manual_required`, with the reason) instead of running it, and `doctor`'s
  `artifacts` check fails. Malformed declarations are errors, never "declares
  nothing". A step that declares `provides` may only record artifacts of those
  types: anything else fails the step.
- **New built-in commands, which yield to a plugin's.** New built-in commands
  `artifacts`, `items`, `events` and `plugins`. A plugin could register any of
  these names before 0.3.0, so a plugin command with one of them is used
  instead of the built-in one, which that profile then does not offer under
  its name (`--help` lists the plugin's command, with the plugin's help). In
  such a profile, scripts that expect the built-in command get the plugin's.
  Other built-in names are unchanged: a plugin command named like one of them
  is ignored with a warning.
- **A new task status, `awaiting_external`** (only reached by workflows with
  an external step), offering `items` and `terminate`, and new event types:
  `item_opened`, `item_claimed`, `item_released`, `item_submitted`,
  `item_rejected` and `children_spawned`. Clients that enumerate statuses or
  event types should accept them.
- **Plugins that require `runspool<0.3` are refused**, as RunSpool refuses any
  plugin outside its declared range: `runspool-wechat` 0.2.0 among them. Use
  `runspool-wechat` 0.2.1, ask a plugin's author for a release that accepts
  0.3, or exempt an exact version in the profile (`allow: [name@version]`).
- **Custom stores** (replacing the `store` seam) need more for each new
  feature: recorded artifacts need `store.artifacts`; external steps need
  `store.items` and `repo.transition(..., also=)`; child tasks need
  `repo.spawner`, `repo.result_writer` and `repo.prepare_updates`; and
  `runspool events` needs `store.log.since`. A store without one keeps working
  until that feature is used, which then fails with an error. This is not a
  complete list: see [docs/upgrading.md](docs/upgrading.md#custom-stores) and
  run `runspool.testing.StoreContract`, which does not check these new
  interfaces yet.

### Fixed
- Crash recovery runs only in the lock holder, so a second process no longer
  requeues work another process is running.
- The daemon never reclaims a task its own process is running, however old the
  task's heartbeat looks (e.g. right after the machine wakes from sleep).

## [0.2.2] - 2026-10-15

### Changed
- `runspool status` lists the newest 20 tasks by default instead of every task,
  with a footer naming unfinished tasks the limit hides. New options: `-n/--limit`,
  `--all`, `--active` (unfinished only) and `--status` (repeatable).
  `status --json` still lists every task unless a filter is given.

- `runspool daemon-status` reports a daemon whose process is suspended (state `T`)
  and exits 1; previously a stopped daemon showed as running. JSON gains `stopped`.

### Fixed
- The daemon points its stdin at /dev/null. Started as a background job
  (`nohup runspool daemon &`; macOS nohup keeps the terminal as stdin), a tool a step
  runs that reads stdin (ffmpeg does by default) got SIGTTIN, which stopped the whole
  job: the daemon still looked alive but scheduled nothing.

## [0.2.1] - 2026-10-04

### Added
- **`daemon/tick` event**: the daemon emits it after every scheduling round with a
  `DaemonTick` (`round`, `now`), so plugins can do periodic maintenance (sweep a
  cache, rotate files) without a process of their own. A listener that raises is
  logged and skipped; scheduling carries on.
- **`default_workflow` setting**: the workflow `runspool add` uses without
  `--workflow` (still `local_file` when unset). `runspool doctor` reports a
  default that no workflow defines.

### Fixed
- `runspool.__version__` said 0.1.0; it now reads the installed version.
- `runspool add` with an unknown workflow printed the error prefix twice
  (`undefined workflow: undefined workflow: 'x'`).

## [0.2.0] - 2026-10-04

RunSpool is now built from plugins on a small kernel. Everything the engine does —
storage, the state machine, scheduling, the CLI, approvals, credentials, built-in
steps — is a plugin, mounted from a profile (the config file). A 0.1 config file
boots unchanged. See [docs/architecture.md](docs/architecture.md) and
[docs/plugins.md](docs/plugins.md).

### Added
- **Plugin kernel** (`runspool.kernel`): plugins declare the services they `inject`
  and load once those are active; everything a plugin registers is undone in
  reverse when it unloads; a failing plugin is rolled back alone; events
  (`emit`, `bail`, `waterfall`); profiles composed from bundles and patches
  (`bundles`, `patch` with `insert` / `disabled` / deep-merged `config`,
  `required`, `allow`); plugin discovery through the `runspool.plugins` and
  `runspool.bundles` entry points with a RunSpool version check; a startup audit
  that names what a pending plugin is waiting for.
- **Core plugins**: `store` (SQLite; replaceable, with a `runspool.testing.StoreContract`
  conformance suite), `steps`, `workflows`, `tasks`, `runtime`, `doctor`,
  `credentials`, `approval`, `cli`; one plugin per built-in step
  (`builtin-archive`, ...), so a profile can replace a single built-in.
- **Approvals for steps with side effects**: a step with `side_effect = True` runs
  only after a human approves that attempt (`runspool approve <id>` /
  `runspool reject <id>`); the task waits in `awaiting_approval` without holding a
  worker. Fail closed: no approval service, policy `never`, or a failing policy
  refuses the step. Plugins can add policies on `step/pre-execute`, and guards
  that can only refuse.
- **Credentials by name** (`credentials` service): config holds names such as
  `WECHAT_APPSECRET`, resolved from the environment, a user credentials file, a
  `.env` next to the profile, or `~/.env`; values never reach logs, events or
  doctor.
- `partially_completed` status: every step ran, but at least one reported
  `StepResult(degraded=True)`; step runs keep a `note`.
- `StepContext.attempt`; `StepDeferred(reason, delay_seconds)` — a deferred task
  waits until its `next_retry_at`, and `runspool wake <id>` makes it runnable now.
- Task `metadata` (JSON) and `parent_task_id`, created atomically with the task
  (`runspool add --parent ID --meta KEY=VALUE`; `tasks.add(..., metadata=, parent=)`).
- `first_task_id` setting: continue an older numbering.
- Plugins contribute CLI subcommands; `runspool` boots the profile before parsing.
- Versioned schema migrations: 0.1 databases upgrade in place; a database from a
  newer RunSpool is refused.
- Official plugin **runspool-wechat** (separate package): lay out Markdown for
  WeChat Official Accounts and save drafts through the official API, behind an
  approval. Example plugin package `runspool-example-creator` (in this repository).
- `docs/design-decisions.md` documenting the *why* behind the architecture
  (local-first SQLite, steps-never-touch-the-DB, centralized state machine,
  atomic claiming, boundary-applied pause/terminate, the agent JSON contract).
- `sample-output/` with committed quickstart output (`inspect --json`,
  `status --json`, and the produced artifacts) so the result is visible without
  running anything.

### Changed
- Task status is written only by the state machine, as compare-and-set
  transitions that record their events in the same transaction.
  `update_fields` (and so `StepResult.updates`) is limited to `name`, `priority`,
  `max_retries`, `progress` and `metadata`.
- One place decides a step boundary: terminate wins over everything; a pause
  after a successful step advances first, after a deferral pauses in place, after
  a failure records it and pauses; a pause or terminate requested before the
  step started skips it. Crash recovery and stale-heartbeat reclaim follow the
  same rules (terminate wins, pause pauses in place), including for
  `pause_pending` tasks.
- `run` and `daemon` refuse to start while an enabled plugin is not active, or a
  lazily loaded step fails to import; read-only commands still work.
- Steps declared in the profile's `steps:` map load lazily, so read-only commands
  never import step code.
- A relative `workspace_root` resolves against the profile's directory; `add`
  stores an existing file's absolute path.
- Task JSON gains `next_retry_at`, `parent_task_id`, `metadata`;
  `available_actions` gains `wake`, `approve`, `reject` where they apply.
- CJK labels are truncated by display width.
- Development tools moved to a PEP 735 dependency group: use `uv sync`.
- Unified maintainer identity to `Ethan Sun <ethan@ethansun.dev>` across the
  license, package metadata, and Code of Conduct contact.
- Standardized the project tooling on [uv](https://docs.astral.sh/uv/):
  install/development docs, CONTRIBUTING, the CI and publish workflows, and the
  example smoke test now use `uv` (`uv tool install`, `uv sync`, `uv run`,
  `uv build`). pip remains a supported end-user install path.

### Breaking
- The Python API of 0.1 (`runspool.app.load_context`, `runspool.runtime`, ...) now
  boots the kernel; code that assembled the engine by hand must use
  `load_context` or a profile. Steps written for 0.1 keep working.
- `StepResult.updates` can no longer set lifecycle columns (status, step, locks,
  retry bookkeeping); such a step now fails.
- Credential names must be upper-case environment-variable style.

### Fixed
- Terminating a task that was mid-pause (`pause_pending`) is no longer
  resurrected as `paused` when the running step finishes: `terminate` now defers
  to the step boundary like `running` and takes precedence over a pending pause.
- `set-retries` now rejects a negative cap (mirroring the config model's `ge=0`),
  instead of silently disabling retries by routing the first failure straight to
  `manual_required`.
- `logs <id>` for a non-existent task now returns `not_found` with a non-zero
  exit (matching `status` and `inspect`), instead of an empty list that could be
  misread as "task exists but has no events".
- `run` now refuses to start while a daemon is active (use `--force` to
  override), preventing it from requeueing the daemon's in-flight tasks and
  executing the same step twice.
- Task claiming is now atomic (a single conditional `UPDATE`), making it safe
  for the daemon and a CLI process to race for the same task.
- `PAUSE_PENDING` tasks now count toward per-step concurrency quotas, so a
  pausing task can no longer let a second instance of a `concurrency: 1` step
  be claimed.
- The runner refreshes a task's heartbeat on a timer while a step runs, so a
  long step that does not call `heartbeat()` is no longer reclaimed as stale
  and run twice.
- Recovery and stale-reclaim now close the interrupted step's `step_run` row
  instead of leaving it stuck in `running`.
- The `archive` step is now crash-safe and idempotent: it preserves the
  previous archive if a move fails and skips re-archiving a completed task.
- SQLite connections now use WAL journaling and a busy timeout to avoid
  `database is locked` errors under concurrent writers.

## [0.1.0] - 2026-06-29

Initial release.

### Added
- Local-first workflow engine backed by SQLite: tasks move through ordered
  steps with a persisted state machine, event log, and per-step run history.
- Full task lifecycle from the CLI: `init`, `add`, `run` (one-shot), `daemon`
  (resident), `status`, `inspect`, `logs`, `overview`, `pause`, `resume`,
  `retry`, `terminate`, `set-priority`, `set-retries`, `set-step`, `workflows`,
  `doctor`.
- `--json` output on every read command, plus an agent-friendly `inspect` that
  returns `available_actions` and a `suggested_next_action`.
- Automatic retries: a failed task is rescheduled (`retry_delay_seconds`) and
  requeued by the coordinator until `max_retries` is reached, then becomes
  `manual_required`.
- Guarded state transitions enforced in the engine (not just the CLI): illegal
  control actions on terminal/incompatible states are refused.
- Graceful pause at step boundaries: a running step always finishes; pause
  advances to the next step (or completes on the last) so no completed step is
  re-run on resume.
- Thread worker pool with per-step concurrency quotas, heartbeats, and stale-task
  reclamation; startup recovery of interrupted tasks.
- Dynamic step plugins loaded from config (`plugin_paths` + `import`), alongside
  five dependency-free built-in steps.
- Three runnable, offline examples: `local-file-pipeline`, `client-intel-brief`,
  and `creator-publishing-pipeline` (draft-only).
- Documentation (`docs/`), English + Simplified Chinese READMEs, and CI running
  ruff, pytest (Python 3.11/3.12/3.13), and an example smoke test.

[Unreleased]: https://github.com/ethan-sun-dev/runspool/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/ethan-sun-dev/runspool/compare/v0.2.2...v0.3.0
[0.2.2]: https://github.com/ethan-sun-dev/runspool/compare/v0.2.1...v0.2.2
[0.2.1]: https://github.com/ethan-sun-dev/runspool/compare/v0.2.0...v0.2.1
[0.2.0]: https://github.com/ethan-sun-dev/runspool/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ethan-sun-dev/runspool/releases/tag/v0.1.0
