"""``cli``: command-line subcommands contributed by plugins.

A plugin adds a command with ``ctx.cli.register(fn)`` (one command, named after the
function or ``name=``) or a group with ``ctx.cli.register(typer.Typer(name=...))``.
The command closes over the plugin's own context, so it uses the plugin's services
directly. ``runspool`` boots the profile before parsing arguments so these commands
exist; a plugin command may not take the name of a built-in command.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from runspool.kernel import Plugin


@dataclass(frozen=True)
class Contributed:
    owner: str
    name: str
    command: Any  # a function or a typer.Typer


class CliService:
    def __init__(self) -> None:
        self.commands: list[Contributed] = []

    def for_context(self, ctx: Any) -> _BoundCli:
        return _BoundCli(self, ctx)


class _BoundCli:
    def __init__(self, service: CliService, ctx: Any) -> None:
        self._service = service
        self._ctx = ctx

    @property
    def commands(self) -> list[Contributed]:
        return list(self._service.commands)

    def register(self, command: Any, *, name: str | None = None) -> Callable[[], None]:
        resolved = name or _name_of(command)
        if not resolved:
            raise ValueError(f"{command!r} needs a name (pass name=...)")
        entry = Contributed(self._ctx.fiber.name, resolved, command)
        commands = self._service.commands

        def register() -> Callable[[], None]:
            if any(c.name == resolved for c in commands):
                raise ValueError(f"command {resolved!r} is already registered")
            commands.append(entry)
            return lambda: commands.remove(entry) if entry in commands else None

        return self._ctx.effect(register, label=f"cli:{resolved}")


def _name_of(command: Any) -> str | None:
    info = getattr(command, "info", None)  # typer.Typer
    if info is not None:
        return getattr(info, "name", None)
    name = getattr(command, "__name__", None)
    return name.replace("_", "-") if name else None


plugin = Plugin(name="cli", apply=lambda ctx, config: ctx.provide("cli", CliService()))
