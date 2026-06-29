# Example: local-file-pipeline

The 3-minute quickstart. It uses only **built-in steps**, so it runs the moment
you install Runspool — no network, API keys, browser, or external tools.

Workflow:

```
ingest_file -> classify_text -> normalize_markdown -> summarize_text -> archive
```

Each task takes one text file from `inbox/` and produces normalized Markdown, a
summary, a classification, and metadata, then archives the package.

## Run it

From this directory:

```bash
# 1. Initialise state for this example (creates ./workspace and the database).
runspool -c config.yaml init --workspace-root ./workspace

# 2. Add one task per input file.
runspool -c config.yaml add ./inbox/invoice.txt
runspool -c config.yaml add ./inbox/support-ticket.txt
runspool -c config.yaml add ./inbox/meeting-notes.txt

# 3. Advance all tasks to completion in one shot.
runspool -c config.yaml run

# 4. See where everything landed.
runspool -c config.yaml status
runspool -c config.yaml inspect 1
```

## Inspect the output (great for scripts and AI agents)

```bash
runspool -c config.yaml inspect 1 --json
runspool -c config.yaml status --json
```

Artifacts for each completed task are under `workspace/ready/<id>/`:

```
workspace/ready/1/
  source.txt          # the ingested input
  metadata.json       # size, line/word counts
  classification.json # invoice | support_ticket | meeting_notes | general
  normalized.md       # cleaned-up Markdown
  summary.md          # extractive summary
  summary.json
```

## Reset

```bash
rm -rf workspace
```
