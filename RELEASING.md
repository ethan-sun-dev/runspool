# Releasing

RunSpool and its official plugins are released together from this repository.
Example plugins (`plugins/runspool-example-*`) are not published.

## 1. Prepare

- Set the version in `pyproject.toml` (RunSpool) and in each published plugin's
  `pyproject.toml` (`plugins/runspool-wechat`). A plugin with no changes may keep its
  version: the publish workflow skips files already on PyPI. That also means a
  plugin changed *without* a version bump is silently not published: check
  `git diff vPREVIOUS -- plugins/<name>` before keeping a version. The workflow
  does fail when the release tag does not match RunSpool's own version. Plugins declare the RunSpool range
  they support, e.g. `runspool>=0.2,<0.3`; RunSpool refuses to load a plugin outside
  its range (a profile can exempt an exact version with `allow`).
- Move the `[Unreleased]` notes in `CHANGELOG.md` under the new version and date,
  and update the compare links at the bottom.
- `uv lock` so the lockfile records the new versions.

## 2. Check

```bash
uv sync
uv run ruff check .
uv run pytest                          # RunSpool and the plugins in plugins/
uv run bash scripts/smoke_examples.sh  # the three examples, end to end
bash scripts/check-downstream.sh       # applications built on this checkout
uv build --package runspool -o dist
uv build --package runspool-wechat -o dist
```

Inspect what was built: each wheel contains only its package, and its metadata
(`unzip -p dist/<wheel> '*/METADATA'`) lists only runtime requirements. Optionally
install the wheels into a fresh virtual environment and run `runspool doctor`
against a profile that mounts the plugin.

## 3. Publish

1. Merge to `main` through a pull request (CI runs lint, tests on Python
   3.11–3.13 and the example smoke test).
2. Tag the merge commit `vX.Y.Z` and push the tag.
3. Create a GitHub release from the tag. The `Publish` workflow builds RunSpool and
   the official plugins and uploads them to PyPI with trusted publishing.
   A package published for the first time needs a *pending publisher* configured
   on PyPI beforehand (project name, this repository, workflow `publish.yml`).
4. Check the release on PyPI and install it: `uv tool install runspool`.
