"""The macOS keychain as a credentials source, read through ``/usr/bin/security``.

A credential is a generic password item: its *service* is the one configured
(default ``runspool``) and its *account* is the credential's name. Store one with::

    security add-generic-password -U -s runspool -a WECHAT_APPSECRET -w

(``-w`` last, with no value: ``security`` prompts for it, so the value is never on
a command line or in shell history.)

A lookup runs ``security find-generic-password -s <service> -a <NAME> -w``. Only
the service and the name are arguments; the value comes back on the child's
standard output, through a pipe only this process reads. Standard input is
``/dev/null``, standard error is discarded, and the child gets a fixed minimal
environment (:data:`SECURITY_ENV`), never this process's own, which may hold
credentials. ``security`` is always run by its absolute path, never looked up on
``PATH``.

What happens:

* the item exists: its value (one trailing newline removed);
* there is no such item (``security`` exits 44): absent, ``None``;
* anything else (not macOS, no ``security``, a locked keychain, access denied, a
  timeout, output that is not UTF-8): :class:`KeychainError`, never a silent
  fallback. Its message holds the name and an exit status at most: never the
  output, never ``security``'s own messages, and the output is gone before it is
  raised: no frame of its traceback holds it in a local variable (some Typer
  versions print those).
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

SECURITY = Path("/usr/bin/security")
ITEM_NOT_FOUND = 44  # errSecItemNotFound, as ``security`` reports it
DEFAULT_SERVICE = "runspool"
DEFAULT_TIMEOUT = 15.0
# All the child gets: none of this process's variables (credentials among them).
# ``security`` finds the user's keychains without HOME.
SECURITY_ENV = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}

_NAME = re.compile(r"^[A-Z_][A-Z0-9_]*$")  # CREDENTIAL_NAME_PATTERN

# (argv, timeout) -> (exit status, standard output). Raises subprocess.TimeoutExpired
# on a timeout and OSError when the program cannot be run.
Runner = Callable[[Sequence[str], float], tuple[int, bytes]]


class KeychainError(RuntimeError):
    """The keychain cannot answer. The message never holds a credential's value."""


def run_security(argv: Sequence[str], timeout: float) -> tuple[int, bytes]:
    out = subprocess.run(
        list(argv),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=dict(SECURITY_ENV),
        timeout=timeout,
        check=False,
    )
    return out.returncode, out.stdout


class MacKeychain:
    """Generic password items of one service, by account (= credential name)."""

    def __init__(
        self,
        service: str = DEFAULT_SERVICE,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        runner: Runner | None = None,
        security: Path = SECURITY,
        platform: str | None = None,
    ) -> None:
        self.service = service
        self.timeout = timeout
        self.security = security
        self._runner = runner or run_security
        self._platform = platform if platform is not None else sys.platform

    def problem(self) -> str | None:
        """Why the keychain cannot be used here, if it cannot."""
        if self._platform != "darwin":
            return f"the macOS keychain is not available on this platform ({self._platform})"
        if not os.access(self.security, os.X_OK):
            return f"{self.security} is not available"
        return None

    def lookup(self, name: str) -> str | None:
        """The value stored for ``name``, or ``None`` if there is no such item."""
        problem = self.problem()
        if problem is not None:
            raise KeychainError(problem)
        if not _NAME.fullmatch(name):  # never echoed: it may be a value passed by mistake
            raise KeychainError("not a valid credential name; the keychain was not asked")
        argv = [str(self.security), "find-generic-password", "-s", self.service, "-a", name, "-w"]
        value, failure = self._ask(name, argv)
        if failure is not None:
            raise KeychainError(failure)
        return value

    def _ask(self, name: str, argv: list[str]) -> tuple[str | None, str | None]:
        """``(value, None)``, ``(None, None)`` for no such item, or ``(None, why)``.

        Never raises for what ``security`` does: the caller raises once this frame,
        and with it the raw output, is gone, so a traceback that shows local
        variables cannot show the output. Nothing is raised inside an except
        block either (a TimeoutExpired holds whatever the child had written)."""
        try:
            status, out = self._runner(argv, self.timeout)
        except subprocess.TimeoutExpired:
            return None, f"keychain lookup of {name} timed out after {self.timeout:g}s"
        except OSError as exc:
            kind = type(exc).__name__
            return None, f"keychain lookup of {name} could not run {self.security} ({kind})"
        if status == ITEM_NOT_FOUND:
            return None, None
        if status != 0:
            return None, f"keychain lookup of {name} failed (security exit status {status})"
        try:
            value = out.decode("utf-8")
        except UnicodeDecodeError:
            return None, f"keychain item {name} is not UTF-8 text"
        if value.endswith("\n"):
            value = value[:-1]
        return value or None, None
