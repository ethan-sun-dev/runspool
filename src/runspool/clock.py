"""Single time source.

All persisted timestamps use UTC text in the same format as SQLite's
``datetime('now')`` so that database-side filtering and Python-side parsing
agree. Display helpers convert to local time only at the presentation layer.
"""

from __future__ import annotations

from datetime import UTC, datetime

_FMT = "%Y-%m-%d %H:%M:%S"


def utcnow_text() -> str:
    """Return the current UTC time as ``YYYY-MM-DD HH:MM:SS`` text."""
    return datetime.now(UTC).strftime(_FMT)


def to_local_text(ts: str | None) -> str:
    """Convert a UTC text timestamp to local-time text for display.

    Storage and filtering stay in UTC; this is presentation only. Empty or
    unparseable input is returned unchanged.
    """
    if not ts:
        return ts or ""
    try:
        dt = datetime.strptime(ts, _FMT).replace(tzinfo=UTC)
    except ValueError:
        return ts
    return dt.astimezone().strftime(_FMT)


def today_text() -> str:
    """Return the local date as ``YYYYMMDD`` for use in file names."""
    return datetime.now().strftime("%Y%m%d")
