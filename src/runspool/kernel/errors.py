"""Kernel exceptions."""

from __future__ import annotations


class KernelError(Exception):
    """Base class for every error raised by the plugin kernel."""


class PluginShapeError(KernelError):
    """An object passed as a plugin is not a function, class, or object with ``apply``."""


class ServiceConflict(KernelError):
    """A service name is already provided in this kernel."""


class ServiceNotInjected(KernelError, AttributeError):
    """A plugin read a service it neither declared in ``inject`` nor provides.

    Subclasses ``AttributeError`` so ``hasattr``/``getattr(..., default)`` keep working.
    """


class InactiveEffectError(KernelError):
    """An effect (registration) was attempted on a fiber that is not live."""


class ComposeError(KernelError):
    """A patch layer is malformed or contradicts the entries it applies to."""


class StartupError(KernelError):
    """A required entry is not active after mounting. ``report`` says why."""

    def __init__(self, message: str, report: object) -> None:
        super().__init__(message)
        self.report = report
