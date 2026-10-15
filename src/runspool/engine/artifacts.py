"""Artifact contracts: typed hand-offs between the steps of a workflow.

A step may declare the artifact types it ``requires`` and ``provides``. A type is
a ``kind`` (``transcript``) or a ``kind:param`` (``variant:blog``). A param written
in angle brackets is a placeholder (``variant:<target>``): it stands for any value.

Matching (does what an earlier step provides satisfy a requirement?):

* ``kind``         is satisfied only by ``kind`` (no param);
* ``kind:<x>``     is satisfied by any ``kind:<something>`` (concrete or placeholder);
* ``kind:value``   is satisfied by ``kind:value`` or by a placeholder ``kind:<x>``,
  which may provide any value.

Placeholder names are labels only; they do not bind across steps. A static check
cannot know which concrete values an application will use, so a placeholder
provider counts as providing all of them; an application that knows its concrete
values can check again with :func:`check_chain` and concrete types.

Contracts are opt-in. A step whose ``requires`` and ``provides`` are both ``None``
(the default) declares nothing and is not checked. A requirement no earlier step
provides is an error, unless an earlier step declares nothing (it might be the
provider): then the requirement is reported as unverified, not as an error.
Conditional steps (``when``) are assumed to run.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

_KIND = re.compile(r"[a-z][a-z0-9_.-]*\Z")
_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\Z")
_PLACEHOLDER = re.compile(r"<[a-z][a-z0-9_]*>\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")


class ArtifactTypeError(ValueError):
    """An artifact type string is malformed."""


@dataclass(frozen=True)
class ArtifactType:
    kind: str
    param: str | None = None

    @property
    def is_placeholder(self) -> bool:
        return self.param is not None and self.param.startswith("<")

    def satisfies(self, required: ArtifactType) -> bool:
        """Whether an artifact of this (provided) type meets ``required``."""
        if self.kind != required.kind:
            return False
        if required.param is None or self.param is None:
            return required.param is None and self.param is None
        if required.is_placeholder or self.is_placeholder:
            return True
        return self.param == required.param

    def __str__(self) -> str:
        return self.kind if self.param is None else f"{self.kind}:{self.param}"


def parse_type(text: str) -> ArtifactType:
    """Parse ``kind`` or ``kind:param``; raise :class:`ArtifactTypeError` if malformed."""
    if not isinstance(text, str):
        raise ArtifactTypeError(f"artifact type must be a string, got {type(text).__name__}")
    kind, sep, param = text.partition(":")
    if not _KIND.match(kind):
        raise ArtifactTypeError(
            f"invalid artifact type {text!r}: the kind must be lower-case letters, digits, "
            "'_', '.' or '-', starting with a letter"
        )
    if not sep:
        return ArtifactType(kind)
    if not (_VALUE.match(param) or _PLACEHOLDER.match(param)):
        raise ArtifactTypeError(
            f"invalid artifact type {text!r}: the param must be a value (letters, digits, "
            "'_', '.', '-') or a placeholder such as <target>"
        )
    return ArtifactType(kind, param)


def parse_types(
    values: Iterable[str] | None, *, owner: str, field_name: str
) -> tuple[ArtifactType, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        raise ArtifactTypeError(f"{owner}: {field_name} must be a list of types, not a string")
    types = []
    for value in values:
        try:
            types.append(parse_type(value))
        except ArtifactTypeError as exc:
            raise ArtifactTypeError(f"{owner}: {field_name}: {exc}") from None
    return tuple(types)


# -- declarations -------------------------------------------------------------------


@dataclass(frozen=True)
class Contract:
    """What one step declares. ``declared`` is False when it declares nothing."""

    requires: tuple[ArtifactType, ...] = ()
    provides: tuple[ArtifactType, ...] = ()
    declared: bool = False


def contract_of(step: Any) -> Contract:
    """Read and parse a step's declarations (raises :class:`ArtifactTypeError`)."""
    requires = getattr(step, "requires", None)
    provides = getattr(step, "provides", None)
    if requires is None and provides is None:
        return Contract()
    name = getattr(step, "name", type(step).__name__)
    return Contract(
        parse_types(requires, owner=f"step {name!r}", field_name="requires"),
        parse_types(provides, owner=f"step {name!r}", field_name="provides"),
        declared=True,
    )


# -- the chain check -----------------------------------------------------------------


@dataclass
class ChainReport:
    """Result of checking one workflow (or all of them)."""

    errors: list[str] = field(default_factory=list)
    unverified: list[str] = field(default_factory=list)
    # Whether anything was declared at all; with nothing declared there is nothing
    # to report, and callers stay silent.
    declared: bool = False

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: ChainReport) -> None:
        self.errors.extend(other.errors)
        self.unverified.extend(other.unverified)
        self.declared = self.declared or other.declared


def check_chain(
    workflow: str,
    steps: Sequence[str],
    contract_for: Callable[[str], Contract | None],
    inputs: Iterable[str] = (),
) -> ChainReport:
    """Check that every type a step requires is provided before it.

    ``contract_for(name)`` returns the step's :class:`Contract`, or ``None`` for a
    step it cannot look at (unregistered, failed to load: other checks report
    those); such a step counts as declaring nothing. It may raise
    :class:`ArtifactTypeError`, which is reported as an error. ``inputs`` are the
    types the task's input provides before the first step.
    """
    report = ChainReport()
    try:
        available = list(
            parse_types(list(inputs), owner=f"workflow {workflow!r}", field_name="inputs")
        )
    except ArtifactTypeError as exc:
        report.errors.append(str(exc))
        available = []
    if available:
        report.declared = True
    providers = {str(t): "the workflow inputs" for t in available}
    silent: list[str] = []  # earlier steps that declare nothing
    for name in steps:
        try:
            contract = contract_for(name)
        except ArtifactTypeError as exc:
            report.declared = True
            report.errors.append(f"workflow {workflow!r}: {exc}")
            silent.append(name)
            continue
        if contract is None or not contract.declared:
            silent.append(name)
            continue
        report.declared = True
        for required in contract.requires:
            if any(have.satisfies(required) for have in available):
                continue
            where = f"workflow {workflow!r}: step {name!r} requires {str(required)!r}"
            if silent:
                report.unverified.append(
                    f"{where}; no earlier step declares it, but "
                    f"{', '.join(repr(s) for s in silent)} declare(s) nothing"
                )
                continue
            offered = ", ".join(f"{t!r} ({p})" for t, p in providers.items()) or "nothing"
            report.errors.append(
                f"{where}, which no earlier step provides (provided before it: {offered})"
            )
        for provided in contract.provides:
            available.append(provided)
            providers.setdefault(str(provided), f"step {name!r}")
    return report


def check_workflows(
    workflows: Mapping[str, Any], contract_for: Callable[[str], Contract | None]
) -> ChainReport:
    """Check every workflow of a config (``{name: WorkflowConfig}``)."""
    report = ChainReport()
    for name, workflow in workflows.items():
        report.extend(
            check_chain(name, workflow.steps, contract_for, getattr(workflow, "inputs", None) or ())
        )
    return report


# -- recorded artifacts --------------------------------------------------------------


@dataclass(frozen=True)
class Artifact:
    """An artifact a step produced, returned in ``StepResult.artifacts``.

    ``type`` must be concrete (no placeholder). ``path`` is where it lives; a
    relative path is relative to ``workspace_root``. With ``sha256`` omitted the
    runner hashes the file (which must then exist and be a file); pass it for an
    artifact the runner cannot read, such as a directory you hashed yourself.
    """

    type: str
    path: str | Path
    sha256: str | None = None


@dataclass(frozen=True)
class RecordedArtifact:
    """A validated artifact, ready to be stored."""

    type: str
    path: str
    sha256: str
    size_bytes: int | None


def prepare_artifacts(
    artifacts: Sequence[Artifact],
    *,
    step: str,
    provides: tuple[ArtifactType, ...] | None,
    workspace_root: Path,
) -> list[RecordedArtifact]:
    """Validate a step's artifacts and hash those without a ``sha256``.

    ``provides`` is the step's declaration (``None``: it declares nothing, so any
    concrete type is accepted). Raises ``ValueError`` naming the artifact by
    position and type (never its path: messages end up in logs).
    """
    prepared = []
    for index, artifact in enumerate(artifacts, 1):
        if not isinstance(artifact, Artifact):
            raise ValueError(f"step {step!r}: artifact {index} is not an Artifact")
        label = f"step {step!r}: artifact {index} ({artifact.type!r})"
        try:
            kind = parse_type(artifact.type)
        except ArtifactTypeError as exc:
            raise ValueError(f"step {step!r}: artifact {index}: {exc}") from None
        if kind.is_placeholder:
            raise ValueError(f"{label}: a recorded artifact needs a concrete type")
        if provides is not None and not any(kind.satisfies(p) for p in provides):
            declared = ", ".join(repr(str(p)) for p in provides) or "nothing"
            raise ValueError(f"{label} is not a type the step provides (it declares {declared})")
        if not isinstance(artifact.path, (str, os.PathLike)) or str(artifact.path) == "":
            raise ValueError(f"{label}: path must be a non-empty str or path")
        if artifact.sha256 is not None and not (
            isinstance(artifact.sha256, str) and _SHA256.match(artifact.sha256.lower())
        ):
            raise ValueError(f"{label}: sha256 must be 64 hex digits")
        path = Path(artifact.path)
        resolved = path if path.is_absolute() else Path(workspace_root) / path
        # Filesystem errors carry the path in their message: report only their class.
        try:
            digest, size = _read(resolved, artifact.sha256, label)
        except OSError as exc:
            raise ValueError(f"{label}: cannot read the file ({type(exc).__name__})") from None
        prepared.append(RecordedArtifact(str(kind), str(artifact.path), digest, size))
    return prepared


def _read(path: Path, sha256: str | None, label: str) -> tuple[str, int | None]:
    """The artifact's digest and size (``None`` when it is not a file)."""
    if sha256 is None:
        if not path.is_file():
            raise ValueError(f"{label}: no file at its path to hash")
        return _hash_file(path)
    return sha256.lower(), path.stat().st_size if path.is_file() else None


def _hash_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size
