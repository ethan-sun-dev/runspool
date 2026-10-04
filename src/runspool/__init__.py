"""Runspool: a local-first CLI workflow engine for reliable personal automation.

Runspool turns local scripts, files, and manual checklists into resumable,
observable workflows backed by SQLite. Tasks move through an ordered list of
steps; every transition is recorded, every step run is timed, and the whole
lifecycle (pause, resume, retry, terminate) is controllable from the CLI and
readable as JSON for scripts and AI agents.
"""

from __future__ import annotations

from importlib import metadata

try:  # the installed distribution is the single source of the version
    __version__ = metadata.version("runspool")
except metadata.PackageNotFoundError:  # a bare source tree
    __version__ = "0+unknown"

__all__ = ["__version__"]
