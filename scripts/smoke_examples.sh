#!/usr/bin/env bash
# Smoke-test all three examples end-to-end exactly as their READMEs describe.
# Fails (non-zero exit) if any example does not reach a completed task. Used by
# CI and runnable locally: `bash scripts/smoke_examples.sh`.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

assert_completed() {
  local dir="$1"
  local status
  status="$(runspool -c config.yaml inspect 1 --json \
    | python3 -c "import json,sys; print(json.load(sys.stdin)['status'])")"
  if [ "$status" != "completed" ]; then
    echo "FAIL [$dir]: task 1 status is '$status', expected 'completed'" >&2
    exit 1
  fi
  echo "OK   [$dir]: task 1 completed"
}

run_example() {
  local dir="$1"; shift
  echo "==> $dir"
  (
    cd "examples/$dir"
    rm -rf workspace
    runspool -c config.yaml init --workspace-root ./workspace >/dev/null
    runspool -c config.yaml add "$@" >/dev/null
    runspool -c config.yaml run >/dev/null
    assert_completed "$dir"
    rm -rf workspace
  )
}

run_example "local-file-pipeline" "./inbox/invoice.txt"
run_example "client-intel-brief" "./data" --workflow client_intel --name "Smoke Test"
run_example "creator-publishing-pipeline" "./materials" --workflow creator_publishing --name "Smoke Test"

echo "All examples passed."
