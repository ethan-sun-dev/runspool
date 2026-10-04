# Contributing to Runspool

Thanks for your interest in improving Runspool! This guide covers how to set up,
make changes, and submit them.

## Principles

Runspool is intentionally small and focused. Before proposing a change, keep the
project's character in mind:

- **Local-first.** No hosted service, no required network access, no telemetry.
- **CLI-first.** The command line and JSON output are the interface. No web UI.
- **Predictable.** State transitions live in one state machine; keep them there.
- **Small surface.** Prefer a plugin over a new core dependency.

See the [non-goals](README.md#non-goals) before suggesting large features.

## Development setup

This project uses [uv](https://docs.astral.sh/uv/) for environment and
dependency management.

```bash
git clone https://github.com/ethan-sun-dev/runspool
cd runspool
uv sync   # creates .venv; installs the project, dev tools and the plugins in plugins/
```

The repository is a uv workspace: `runspool` itself, plus the plugin packages in
[`plugins/`](plugins/) (`runspool-wechat`, `runspool-example-creator`), installed
in editable mode by `uv sync`. Dev tools live in a PEP 735 dependency group, so
the published package carries no dev requirements. Run the CLI from the checkout
with `uv run runspool ...`.

## Run the checks

```bash
uv run ruff check .                     # lint
uv run pytest                           # tests: tests/ and plugins/*/tests
uv run bash scripts/smoke_examples.sh   # run the three examples end to end
```

All three must pass before you open a pull request; CI runs the same commands
on Python 3.11–3.13. The smoke script runs each example exactly as its README
describes and fails unless the task completes.

## Making changes

- **Match the surrounding style.** The codebase favors small, single-purpose
  modules and clear comments explaining *why*, not *what*.
- **Write tests.** New behavior needs a test; bug fixes need a regression test.
  See [`tests/`](tests/) for patterns (fixtures live in `tests/conftest.py`).
- **Keep the engine generic.** Kernel, engine, persistence, and CLI code must not
  depend on any specific domain. Domain logic belongs in steps and plugins.
- **Keep status in the state machine.** Task status and the other lifecycle
  columns change only through the state machine's compare-and-set transitions;
  steps and plugins never write them.
- **Update docs.** If you change the CLI, the step contract or the plugin API,
  update the relevant file under [`docs/`](docs/) and the README.
- **English everywhere.** Code, comments, config, help text, and docs are in
  English.

## Adding a step

Most new capabilities should be a step or a plugin, not engine changes. See
[docs/writing-steps.md](docs/writing-steps.md) and
[docs/plugins.md](docs/plugins.md). If a step is broadly useful and
dependency-free, it may belong in `src/runspool/builtin_steps/` (registered as a
`builtin-*` entry); otherwise it's a great fit for an example or your own plugin
package. A plugin in `plugins/` keeps its tests in its own `tests/` directory,
which `uv run pytest` picks up.

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
