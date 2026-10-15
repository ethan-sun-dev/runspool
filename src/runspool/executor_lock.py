"""Executor lock: at most one process executes steps against a database at a time.

An application may run the engine in-process, and several short-lived processes
(a CLI, a UI, an agent's tool call) can exist at once. Only the process holding the
executor lock runs steps; crash recovery and schema upgrades also happen only while
holding it, so no process requeues work another process is running.

The lock is an OS file lock (``flock`` on POSIX, ``msvcrt.locking`` on Windows) on a
file next to the database (``runspool.db`` -> ``runspool.db.executor-lock``): it is
per database, whichever config opened it. The OS drops it when the holder exits,
however it exits, so there is no stale lock to clean up. The holder writes its pid
into the file for diagnostics only; the pid never decides who holds the lock.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

LOCK_SUFFIX = ".executor-lock"


def executor_lock_path(database_path: Path | str) -> Path:
    path = Path(database_path)
    return path.with_name(path.name + LOCK_SUFFIX)


class ExecutorBusy(RuntimeError):
    """Another process holds the executor lock."""

    def __init__(self, message: str, holder: int | None = None) -> None:
        super().__init__(message)
        self.holder = holder


class ExecutorLock:
    """A non-blocking, process-wide lock. Not re-entrant: acquire it once per process.

    The lock belongs to the open file, so a second ``ExecutorLock`` on the same path
    in the same process does not get it either.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self._file: IO[str] | None = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self) -> bool:
        """Take the lock if it is free; return whether this object now holds it."""
        if self._file is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # "a+" creates the file without truncating it: the holder's pid stays
        # readable while someone else only tries the lock.
        f = open(self.path, "a+", encoding="utf-8")  # noqa: SIM115 - held until release
        try:
            if not _try_lock(f):
                f.close()
                return False
            f.seek(0)
            f.truncate()
            f.write(str(os.getpid()))
            f.flush()
        except BaseException:
            f.close()
            raise
        self._file = f
        return True

    def release(self) -> None:
        f, self._file = self._file, None
        if f is None:
            return
        try:
            f.seek(0)
            f.truncate()
            f.flush()
            _unlock(f)
        finally:
            f.close()

    def holder(self) -> int | None:
        """The pid last written by a holder, if any (diagnostics only)."""
        try:
            text = self.path.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        return int(text) if text.isdigit() else None

    def busy_error(self, what: str) -> ExecutorBusy:
        pid = self.holder()
        who = f"another process (pid {pid})" if pid else "another process"
        return ExecutorBusy(f"{what}: {who} is executing tasks ({self.path})", pid)

    def __enter__(self) -> ExecutorLock:
        if not self.acquire():
            raise self.busy_error("cannot take the executor lock")
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


try:
    import fcntl

    def _try_lock(f: IO[str]) -> bool:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        return True

    def _unlock(f: IO[str]) -> None:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)

except ImportError:  # Windows
    import msvcrt

    def _try_lock(f: IO[str]) -> bool:
        f.seek(0)
        try:
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(f: IO[str]) -> None:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
