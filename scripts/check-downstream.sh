#!/usr/bin/env bash
# Run the test suites of applications built on this RunSpool checkout, before you
# push a change to RunSpool. Downstream projects depend on RunSpool through an
# editable path, so their tests exercise exactly this working tree.
#
# Downstream projects are listed, one directory per line, in `.downstream` at the
# repository root (git-ignored: the paths are yours), or in RUNSPOOL_DOWNSTREAM
# separated by ':'. Relative paths are relative to the repository root. Each one
# is tested with `uv run pytest` in its own environment.
#
#   echo ../my-app >> .downstream
#   bash scripts/check-downstream.sh
set -uo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

projects=()
if [ -n "${RUNSPOOL_DOWNSTREAM:-}" ]; then
  IFS=':' read -r -a projects <<< "$RUNSPOOL_DOWNSTREAM"
elif [ -f .downstream ]; then
  while IFS= read -r line; do
    line="${line%%#*}"
    line="$(echo "$line" | xargs)"
    [ -n "$line" ] && projects+=("$line")
  done < .downstream
fi

if [ "${#projects[@]}" -eq 0 ]; then
  echo "No downstream projects configured (.downstream or RUNSPOOL_DOWNSTREAM); nothing to check."
  exit 0
fi

failed=()
for project in "${projects[@]}"; do
  dir="$project"
  [[ "$dir" = /* ]] || dir="$repo_root/$dir"
  if [ ! -f "$dir/pyproject.toml" ]; then
    echo "SKIP [$project]: no pyproject.toml"
    failed+=("$project (missing)")
    continue
  fi
  echo "==> $project"
  if (cd "$dir" && uv run pytest -q); then
    echo "OK   [$project]"
  else
    echo "FAIL [$project]"
    failed+=("$project")
  fi
done

if [ "${#failed[@]}" -gt 0 ]; then
  echo "Downstream failures: ${failed[*]}" >&2
  exit 1
fi
echo "All downstream projects passed."
