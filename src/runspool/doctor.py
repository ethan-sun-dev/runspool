"""doctor: check the local environment Runspool needs to run.

The checks themselves live in the ``doctor`` core plugin (runspool.core.doctor);
plugins can add their own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from runspool.core.doctor import Check

if TYPE_CHECKING:
    from runspool.app import AppContext

__all__ = ["Check", "run_doctor"]


def run_doctor(ctx: AppContext) -> list[Check]:
    return ctx.service("doctor").run()
