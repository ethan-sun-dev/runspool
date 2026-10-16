"""External steps: work done outside the engine (by an AI agent, by a person).

An external step does not run code to do its work. When a task reaches it, the
engine opens a *work item* (what to do, from :meth:`ExternalStep.request`) and the
task waits in ``awaiting_external``. A client lists open items, claims one (only
one claimant wins), and submits a result. The task is then queued again, and the
executor (``run`` or the daemon, after the usual start-up checks) passes the
submission to the step's :meth:`ExternalStep.validate`:

* it returns a :class:`~runspool.engine.step.StepResult`: the submission is
  accepted and the workflow advances, exactly as if the step had run and returned
  that result (``updates``, ``artifacts``, ``degraded`` and ``message`` apply);
* it raises :class:`SubmissionRejected`: that attempt is used up. A new item is
  opened for the next attempt, carrying the reason as ``previous_error``; after
  ``max_attempts`` rejections the task goes to ``manual_required`` instead
  (``runspool retry`` starts over with a fresh count);
* it raises anything else: a step failure, retried like any other; the submission
  is kept and validated again.

Validation never runs in the client that submits: submitting only records the
submission. Steps that are not external are untouched by any of this.

Example::

    class Review(ExternalStep):
        name = "review"
        max_attempts = 3

        def request(self, ctx):
            return WorkRequest(kind="human", instructions="Proofread the draft.",
                               payload={"draft": ctx.task["input"]})

        def validate(self, ctx, submission):
            path = submission.file(ctx.config.workspace_root)  # checks sha256 if given
            if not path.read_text(encoding="utf-8").strip():
                raise SubmissionRejected("the file is empty")
            return StepResult(message="reviewed")
"""

from __future__ import annotations

import hashlib
from abc import abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from runspool.engine.step import Step, StepContext, StepResult


@dataclass(frozen=True)
class WorkRequest:
    """What an external step asks for. ``kind`` is a free label for clients
    (``"agent"``, ``"human"``, ...); ``payload`` is a JSON object they can read."""

    kind: str = "external"
    instructions: str = ""
    payload: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Submission:
    """A submitted result, as passed to :meth:`ExternalStep.validate`.

    ``value`` is the JSON object the client submitted. The command line submits
    ``{"path": ..., "sha256": ...}`` for ``--file`` and ``{"value": ...}`` for
    ``--value``; other clients may submit any object.
    """

    item_id: int
    attempt: int
    value: Mapping[str, Any]
    submitted_by: str | None = None

    def file(self, workspace_root: Path | str | None = None) -> Path:
        """The submitted file (``value["path"]``, relative paths against
        ``workspace_root``). Rejects the submission if there is no path, the file
        does not exist, or a submitted ``sha256`` does not match its content.
        Reasons never include the path."""
        raw = self.value.get("path")
        if not isinstance(raw, str) or not raw:
            raise SubmissionRejected("the submission has no file path")
        path = Path(raw).expanduser()
        if not path.is_absolute() and workspace_root is not None:
            path = Path(workspace_root) / path
        if not path.is_file():
            raise SubmissionRejected("the submitted file does not exist")
        expected = self.value.get("sha256")
        if expected is not None:
            if not isinstance(expected, str) or not expected:
                raise SubmissionRejected("the submitted sha256 is malformed")
            if sha256_of(path) != expected.lower():
                raise SubmissionRejected("the submitted file does not match its sha256")
        return path


class SubmissionRejected(Exception):
    """Raised by :meth:`ExternalStep.validate`: the submission does not do the job.

    ``reason`` is recorded on the item and the task, and handed to the next attempt
    as ``previous_error``: say what is wrong so it can be fixed.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ExternalStep(Step):
    """A step done outside the engine; see the module docstring."""

    external = True
    # Rejected submissions allowed before the task needs a human (manual_required).
    max_attempts: int = 3

    def request(self, ctx: StepContext) -> WorkRequest:
        """What the work item asks for. ``ctx.attempt`` is the item's attempt."""
        return WorkRequest()

    @abstractmethod
    def validate(self, ctx: StepContext, submission: Submission) -> StepResult:
        """Accept the submission (return a result) or raise SubmissionRejected."""

    def run(self, ctx: StepContext) -> StepResult:
        raise TypeError(f"external step {self.name!r} is not run; it opens a work item")


def is_external(step: Any) -> bool:
    return bool(getattr(step, "external", False))


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()
