"""Runtime entry points for the CLI: one-shot `run`, daemon, and daemon process helpers.

The work is done by the ``runtime`` core plugin (runspool.core.runtime); these
functions take an application context and delegate to it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from runspool.app import AppContext
from runspool.daemon import Daemon


def build_daemon(ctx: AppContext) -> Daemon:
    return ctx.service("runtime").build_daemon()


def run_until_idle(
    ctx: AppContext,
    *,
    notifier: Callable[[str], None] | None = None,
    max_rounds: int | None = None,
) -> int:
    """Advance every runnable task until no further progress is made."""
    return ctx.service("runtime").run_until_idle(notifier=notifier, max_rounds=max_rounds)


def daemon_pid_file(ctx: AppContext) -> Path:
    return ctx.service("runtime").pid_file()


def daemon_status(ctx: AppContext) -> dict[str, Any]:
    return ctx.service("runtime").daemon_status()


def request_daemon_stop(ctx: AppContext) -> bool:
    return ctx.service("runtime").request_daemon_stop()
