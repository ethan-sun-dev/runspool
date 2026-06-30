# Sample output

Committed output from the [`local-file-pipeline` example](../examples/local-file-pipeline/) —
so you can see exactly what Runspool produces **without installing or running anything**.

It is the result of feeding one `invoice.txt` through the default `local_file`
workflow (`ingest_file → classify_text → normalize_markdown → summarize_text → archive`):

```bash
runspool -c config.yaml init --workspace-root ./workspace
runspool -c config.yaml add ./inbox/invoice.txt
runspool -c config.yaml run
runspool -c config.yaml inspect 1 --json   # → inspect-1.json
runspool -c config.yaml status   --json    # → status.json
```

## What's here

| File | What it is |
| --- | --- |
| [`inspect-1.json`](inspect-1.json) | Agent-friendly snapshot of task 1: status, recent events, per-step timing, artifact list, and a suggested next action. |
| [`status.json`](status.json) | The full task list as JSON (one row per task). |
| [`ready/1/`](ready/1/) | The artifacts the workflow wrote for task 1. |

## The artifacts (`ready/1/`)

| File | Produced by | Contents |
| --- | --- | --- |
| [`source.txt`](ready/1/source.txt) | `ingest_file` | The original input, copied into the task workspace. |
| [`classification.json`](ready/1/classification.json) | `classify_text` | Detected category + matched keywords + confidence. |
| [`normalized.md`](ready/1/normalized.md) | `normalize_markdown` | Cleaned-up Markdown. |
| [`summary.md`](ready/1/summary.md) / [`summary.json`](ready/1/summary.json) | `summarize_text` | Human- and machine-readable summary. |
| [`metadata.json`](ready/1/metadata.json) | `archive` | Size, line/word/char counts, original path. |

The two interesting signals for someone reading this repo:

1. **`inspect-1.json` is a contract, not a log dump.** It gives a script or AI
   agent everything needed to decide what to do next — including
   `available_actions` and `suggested_next_action` — in one call.
2. **`step_runs` carries per-step timing.** Every step is individually timed and
   recorded, so failures are attributable to a specific step, not the task as a whole.

> Timestamps in these files are from the run that generated them; re-running the
> example reproduces the same structure with fresh times.
