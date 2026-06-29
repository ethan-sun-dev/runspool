# Example: client-intel-brief

A real-world business case: turn a pile of client, competitor, and customer
sources into a structured **briefing package**. This is the kind of work an
independent consultant or a market/competitive researcher does by hand.

It shows Runspool doing more than a toy demo, and it demonstrates **custom
steps** loaded as plugins (see [steps/intel_steps.py](steps/intel_steps.py)).
Everything is offline — the inputs are sample Markdown files.

Workflow:

```
collect_sources -> extract_signals -> map_competitors -> identify_opportunities -> write_brief -> archive
```

(The first five steps are custom plugins; `archive` is built in.)

## Run it

From this directory:

```bash
runspool -c config.yaml init --workspace-root ./workspace
runspool -c config.yaml add ./data --workflow client_intel --name "Northwind Trading"
runspool -c config.yaml run
runspool -c config.yaml inspect 1
```

Outputs land in `workspace/ready/1/`:

```
client-intel-brief.md   # the assembled brief
opportunity-map.md      # requirements vs. competitor coverage
source-index.json       # everything that was collected
signals.json
competitors.json
sources/                # a copy of the inputs
```

## See the manual_required / agent loop

Remove a required source and watch the task ask for help instead of failing
silently:

```bash
mv ./data/requirements.md /tmp/requirements.md
runspool -c config.yaml add ./data --workflow client_intel --name "Broken run"
runspool -c config.yaml run
runspool -c config.yaml inspect 2 --json
```

`inspect ... --json` reports `status: "manual_required"`, a `last_error`
naming the missing file, the `available_actions`, and a `suggested_next_action`
telling you (or an AI agent) exactly how to recover:

```bash
mv /tmp/requirements.md ./data/requirements.md   # fix the cause
runspool -c config.yaml retry 2                  # as suggested
runspool -c config.yaml run
```

## Reset

```bash
rm -rf workspace
```
