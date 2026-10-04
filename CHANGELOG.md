# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.2.0] - TBD

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

[Unreleased]: https://github.com/ethan-sun-dev/runspool/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ethan-sun-dev/runspool/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/ethan-sun-dev/runspool/releases/tag/v0.1.0
