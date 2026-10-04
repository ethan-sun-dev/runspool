"""The bundles RunSpool ships: ``core`` (the engine) and ``builtin-steps``.

A profile that lists no bundles gets both. Entry ids are stable: a profile patches
them by id (``{id: builtin-archive, disabled: true}``).
"""

from __future__ import annotations

# Services every RunSpool needs. "store" and "credentials" are seams (replaceable);
# the rest are core.
CORE_SERVICES = ("store", "credentials", "steps", "workflows", "tasks", "runtime", "doctor")
# Core entries that may not be disabled: they carry the engine's invariants.
LOCKED_CORE_IDS = ("steps", "workflows", "tasks", "runtime", "doctor")

CORE = [
    {
        "insert": [
            {"id": "store", "plugin": "runspool.core.store:plugin"},
            {"id": "steps", "plugin": "runspool.core.steps:plugin"},
            {"id": "workflows", "plugin": "runspool.core.workflows:plugin"},
            {"id": "tasks", "plugin": "runspool.core.tasks:plugin"},
            {"id": "runtime", "plugin": "runspool.core.runtime:plugin"},
            {"id": "doctor", "plugin": "runspool.core.doctor:plugin"},
            {"id": "credentials", "plugin": "runspool.core.credentials:plugin"},
        ]
    }
]

BUILTIN_STEPS = [
    {
        "insert": [
            {"id": f"builtin-{name}", "plugin": f"runspool.core.builtin:{name}"}
            for name in (
                "ingest_file",
                "classify_text",
                "normalize_markdown",
                "summarize_text",
                "archive",
            )
        ]
    }
]

DEFAULT_BUNDLES = ("core", "builtin-steps")
