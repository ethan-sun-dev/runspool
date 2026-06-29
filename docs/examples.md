# Examples

Three runnable examples ship with Runspool. Each has its own directory, config,
sample data, and README. Run them from inside their directory with
`-c config.yaml`.

## 1. local-file-pipeline — the quickstart

[examples/local-file-pipeline/](../examples/local-file-pipeline/)

Built-in steps only; no network, API keys, or external tools. Processes text
files into normalized Markdown, a summary, a classification, and metadata.

```bash
cd examples/local-file-pipeline
runspool -c config.yaml init --workspace-root ./workspace
runspool -c config.yaml add ./inbox/invoice.txt
runspool -c config.yaml run
runspool -c config.yaml inspect 1
```

Use it to confirm your install works and to see the CLI, SQLite state, logs,
artifacts, and JSON output in action.

## 2. client-intel-brief — a real business case

[examples/client-intel-brief/](../examples/client-intel-brief/)

A consulting / competitive-research workflow: collect client, competitor, and
customer sources and assemble a briefing package. Demonstrates **custom plugin
steps** and the **manual_required recovery loop** (remove a required source and
watch `inspect --json` tell you exactly how to fix it).

```bash
cd examples/client-intel-brief
runspool -c config.yaml init --workspace-root ./workspace
runspool -c config.yaml add ./data --workflow client_intel --name "Northwind Trading"
runspool -c config.yaml run
runspool -c config.yaml inspect 1
```

## 3. creator-publishing-pipeline — an advanced content pipeline

[examples/creator-publishing-pipeline/](../examples/creator-publishing-pipeline/)

Builds a multi-platform **draft** package (article + WeChat HTML + X thread +
LinkedIn post + Bilibili description) plus a publish checklist and manifest. It
is deliberately **draft-only** and never auto-publishes.

```bash
cd examples/creator-publishing-pipeline
runspool -c config.yaml init --workspace-root ./workspace
runspool -c config.yaml add ./materials --workflow creator_publishing --name "Local-first automation"
runspool -c config.yaml run
```

Output lands in `workspace/ready/1/dist/`.

## Reset any example

```bash
rm -rf workspace
```

## Learn from the plugin code

Examples 2 and 3 are the best reference for writing your own steps:

- [intel_steps.py](../examples/client-intel-brief/steps/intel_steps.py)
- [creator_steps.py](../examples/creator-publishing-pipeline/steps/creator_steps.py)

See [writing-steps.md](writing-steps.md) for the step contract.
