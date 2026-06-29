---
name: Bug report
about: Report a problem with Runspool
title: "[Bug] "
labels: bug
---

Thanks for taking the time to report a bug. Please fill out the sections below so we can reproduce and fix the issue quickly.

## Describe the bug

A clear and concise description of what the bug is.

## To reproduce

Steps to reproduce the behavior:

1. Initialize a workspace
2. Queue a task
3. Advance the workflow
4. Observe the error

```bash
runspool init
echo "sample input" > sample.txt
runspool add ./sample.txt
runspool run
runspool inspect 1 --json
```

## Expected behavior

A clear and concise description of what you expected to happen.

## Actual behavior

A clear and concise description of what actually happened. Where possible,
include the output of `runspool inspect <id> --json` for the affected run or
step so we can see the full state.

## Environment

- OS: (e.g. macOS 14.5, Ubuntu 22.04, Windows 11)
- Python version: output of `python --version`
- Runspool version: output of `runspool --help` (version line) or `pip show runspool`

## Additional context

Add any other context about the problem here (logs, config files, screenshots).
