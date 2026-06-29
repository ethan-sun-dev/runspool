# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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

[0.1.0]: https://github.com/ethan-sun-dev/runspool/releases/tag/v0.1.0
