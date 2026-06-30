# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

### Added
- `docs/design-decisions.md` documenting the *why* behind the architecture
  (local-first SQLite, steps-never-touch-the-DB, centralized state machine,
  atomic claiming, boundary-applied pause/terminate, the agent JSON contract).
- `sample-output/` with committed quickstart output (`inspect --json`,
  `status --json`, and the produced artifacts) so the result is visible without
  running anything.

### Changed
- Unified maintainer identity to `Ethan Sun <ethan@ethansun.dev>` across the
  license, package metadata, and Code of Conduct contact.
- Standardized the project tooling on [uv](https://docs.astral.sh/uv/):
  install/development docs, CONTRIBUTING, the CI and publish workflows, and the
  example smoke test now use `uv` (`uv tool install`, `uv sync`, `uv run`,
  `uv build`). pip remains a supported end-user install path.

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

[Unreleased]: https://github.com/ethan-sun-dev/runspool/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/ethan-sun-dev/runspool/releases/tag/v0.1.0
