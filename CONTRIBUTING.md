# Contributing to Runspool

Thanks for your interest in improving Runspool! This guide covers how to set up,
make changes, and submit them.

## Principles

Runspool is intentionally small and focused. Before proposing a change, keep the
project's character in mind:

- **Local-first.** No hosted service, no required network access, no telemetry.
- **CLI-first.** The command line and JSON output are the interface. No web UI.
- **Predictable.** State transitions live in one state machine; keep them there.
- **Small surface.** Prefer a plugin step over a new core dependency.

See the [non-goals](README.md#non-goals) before suggesting large features.

## Development setup

```bash
git clone https://github.com/ethan-sun-dev/runspool
cd runspool
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Run the checks

```bash
ruff check .        # lint
pytest              # tests
```

Both must pass before you open a pull request. To try the CLI end-to-end, run
the [local-file-pipeline example](examples/local-file-pipeline/).

## Making changes

- **Match the surrounding style.** The codebase favors small, single-purpose
  modules and clear comments explaining *why*, not *what*.
- **Write tests.** New behavior needs a test; bug fixes need a regression test.
  See [`tests/`](tests/) for patterns (fixtures live in `tests/conftest.py`).
- **Keep the engine generic.** Engine, persistence, and CLI code must not depend
  on any specific domain. Domain logic belongs in steps (built-in or plugin).
- **Update docs.** If you change the CLI or the step contract, update the
  relevant file under [`docs/`](docs/) and the README.
- **English everywhere.** Code, comments, config, help text, and docs are in
  English.

## Adding a step

Most new capabilities should be a step, not engine changes. See
[docs/writing-steps.md](docs/writing-steps.md). If a step is broadly useful and
dependency-free, it may belong in `src/runspool/builtin_steps/`; otherwise it's
a great fit for an example or your own plugin.

## Commit and PR

- Keep commits focused with clear messages.
- Fill in the pull request template (summary, related issue, checklist).
- Link any relevant issue.

## Reporting bugs and requesting features

Use the issue templates. For bugs, include your OS, Python version, Runspool
version, the commands you ran, and `runspool inspect <id> --json` output where
relevant.

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md). By
participating, you agree to uphold it.
