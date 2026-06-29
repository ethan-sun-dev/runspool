"""Task workspace helpers.

The filesystem is the artifact store. Each task gets an isolated directory under
``<workspace_root>/tasks/<id>/``; steps read and write files there.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def task_workspace(config: Any, task: dict[str, Any]) -> Path:
    """Return (creating if needed) the working directory for a task."""
    ws = Path(config.workspace_root) / "tasks" / str(task["id"])
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def archive_dir(config: Any, task: dict[str, Any]) -> Path:
    """Return the destination directory for an archived task."""
    return Path(config.workspace_root) / "ready" / str(task["id"])


def read_source(ws: Path) -> str:
    """Read the canonical source text written by the intake step."""
    return (ws / "source.txt").read_text(encoding="utf-8")


def list_artifacts(config: Any, task: dict[str, Any]) -> list[str]:
    """List artifact files for a task as paths relative to ``workspace_root``.

    Looks in both the active task directory and the archived directory, so a
    completed (archived) task still reports its outputs.
    """
    root = Path(config.workspace_root)
    found: list[str] = []
    for base in (root / "tasks" / str(task["id"]), root / "ready" / str(task["id"])):
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file():
                found.append(str(path.relative_to(root)))
    return found
