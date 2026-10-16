"""Work items of external steps (see runspool.engine.external), one row per attempt.

Status: ``open`` -> ``claimed`` -> ``submitted`` -> ``accepted`` | ``rejected`` |
``exhausted`` (rejected with no attempts left); ``cancelled`` when the task moves on
without it (terminated, or set to another step). A claimed item goes back to
``open`` when released. At most one item per task is active (open, claimed or
submitted); the database enforces it.

Claiming is a single conditional UPDATE, so of two clients racing for one item
exactly one wins. Changes that go together with a task transition (opening an
item, submitting, accepting, rejecting, cancelling) are returned as hooks that the
state machine runs inside that transition's transaction.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from runspool.models import EventType, TaskStatus
from runspool.persistence.connection import Database
from runspool.persistence.event_log import Event, insert_event

ACTIVE = ("open", "claimed", "submitted")
STATUSES = ("open", "claimed", "submitted", "accepted", "rejected", "exhausted", "cancelled")

Hook = Callable[[sqlite3.Connection], None]


class ItemConflict(RuntimeError):
    """The work item is not in the state the change needs (someone else changed it)."""


class WorkItems:
    def __init__(self, db: Database) -> None:
        self.db = db

    # -- reads --------------------------------------------------------------------

    def get(self, item_id: int) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute("select * from work_items where id = ?", (item_id,)).fetchone()
            return _row(row)

    def active_for(self, task_id: int) -> dict[str, Any] | None:
        """The task's active item (open, claimed or submitted), if any."""
        with self.db.connect() as conn:
            row = conn.execute(
                f"select * from work_items where task_id = ? and status in ({_marks(ACTIVE)})",
                (task_id, *ACTIVE),
            ).fetchone()
            return _row(row)

    def latest_for(self, task_id: int, step: str) -> dict[str, Any] | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "select * from work_items where task_id = ? and step = ? order by id desc limit 1",
                (task_id, step),
            ).fetchone()
            return _row(row)

    def list_items(
        self,
        *,
        statuses: tuple[str, ...] | None = None,
        task_id: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Items, oldest first; optionally only some statuses or one task's."""
        sql, params = "select * from work_items where 1 = 1", []
        if statuses is not None:
            sql += f" and status in ({_marks(statuses)})"
            params.extend(statuses)
        if task_id is not None:
            sql += " and task_id = ?"
            params.append(task_id)
        sql += " order by id asc"
        if limit is not None:
            sql += " limit ?"
            params.append(limit)
        with self.db.connect() as conn:
            return [_row(r) for r in conn.execute(sql, params).fetchall()]

    # -- client actions (no task transition) --------------------------------------

    def claim(self, item_id: int, *, by: str) -> str | None:
        """Claim an open item whose task is waiting on it. Returns the claim token
        (needed to submit or release), or ``None`` if someone else got it first or
        the item is not open."""
        token = uuid4().hex
        with self.db.connect() as conn:
            cur = conn.execute(
                "update work_items set status = 'claimed', claimed_by = ?, claim_token = ?, "
                "claimed_at = datetime('now'), updated_at = datetime('now') "
                "where id = ? and status = 'open' and exists (select 1 from tasks t "
                "where t.id = work_items.task_id and t.task_status = ?)",
                (by, token, item_id, TaskStatus.AWAITING_EXTERNAL),
            )
            if cur.rowcount != 1:
                return None
            item = conn.execute(
                "select task_id, step from work_items where id = ?", (item_id,)
            ).fetchone()
            insert_event(
                conn,
                item["task_id"],
                Event(
                    EventType.ITEM_CLAIMED,
                    step=item["step"],
                    message=f"item {item_id}",
                    payload={"item_id": item_id, "by": by},
                ),
            )
            return token

    def release(self, item_id: int, *, token: str | None) -> bool:
        """Give a claimed item back (``open``). With ``token=None`` any claim is
        released (an operator freeing an item whose claimant went away)."""
        sql = (
            "update work_items set status = 'open', claimed_by = null, claim_token = null, "
            "claimed_at = null, updated_at = datetime('now') where id = ? and status = 'claimed'"
        )
        params: list[Any] = [item_id]
        if token is not None:
            sql += " and claim_token = ?"
            params.append(token)
        with self.db.connect() as conn:
            cur = conn.execute(sql, params)
            if cur.rowcount != 1:
                return False
            item = conn.execute(
                "select task_id, step from work_items where id = ?", (item_id,)
            ).fetchone()
            insert_event(
                conn,
                item["task_id"],
                Event(
                    EventType.ITEM_RELEASED,
                    step=item["step"],
                    message=f"item {item_id}",
                    payload={"item_id": item_id},
                ),
            )
            return True

    # -- hooks run inside a task transition ---------------------------------------

    def opener(
        self,
        task_id: int,
        step: str,
        *,
        attempt: int,
        max_attempts: int,
        kind: str,
        instructions: str,
        payload: Mapping[str, Any],
        previous_error: str | None,
    ) -> Hook:
        payload_json = json.dumps(dict(payload), ensure_ascii=False, sort_keys=True)

        def hook(conn: sqlite3.Connection) -> None:
            conn.execute(
                "insert into work_items (task_id, step, attempt, max_attempts, status, kind, "
                "instructions, payload_json, previous_error) values (?,?,?,?,'open',?,?,?,?)",
                (
                    task_id,
                    step,
                    attempt,
                    max_attempts,
                    kind,
                    instructions,
                    payload_json,
                    previous_error,
                ),
            )

        return hook

    def submitter(
        self, item_id: int, *, token: str, value: Mapping[str, Any], by: str | None
    ) -> Hook:
        value_json = json.dumps(dict(value), ensure_ascii=False, sort_keys=True)

        def hook(conn: sqlite3.Connection) -> None:
            cur = conn.execute(
                "update work_items set status = 'submitted', submission_json = ?, "
                "submitted_by = ?, submitted_at = datetime('now'), updated_at = datetime('now') "
                "where id = ? and status = 'claimed' and claim_token = ?",
                (value_json, by, item_id, token),
            )
            if cur.rowcount != 1:
                raise ItemConflict(f"work item {item_id} is not claimed with that token")

        return hook

    def closer(
        self, item_id: int, *, status: str, error: str | None = None, step_run_id: int | None
    ) -> Hook:
        """Close a submitted item: ``accepted``, ``rejected`` or ``exhausted``."""

        def hook(conn: sqlite3.Connection) -> None:
            cur = conn.execute(
                "update work_items set status = ?, error = ?, step_run_id = ?, "
                "updated_at = datetime('now') where id = ? and status = 'submitted'",
                (status, error, step_run_id, item_id),
            )
            if cur.rowcount != 1:
                raise ItemConflict(f"work item {item_id} is no longer submitted")

        return hook

    def canceller(self, task_id: int, *, keep_step: str | None = None) -> Hook:
        """Cancel the task's active items (except those of ``keep_step``)."""

        def hook(conn: sqlite3.Connection) -> None:
            sql = (
                "update work_items set status = 'cancelled', updated_at = datetime('now') "
                f"where task_id = ? and status in ({_marks(ACTIVE)})"
            )
            params: list[Any] = [task_id, *ACTIVE]
            if keep_step is not None:
                sql += " and step != ?"
                params.append(keep_step)
            conn.execute(sql, params)

        return hook


def chain(*hooks: Hook | None) -> Hook | None:
    """One hook running ``hooks`` in order (``None`` entries skipped)."""
    present = [h for h in hooks if h is not None]
    if not present:
        return None
    if len(present) == 1:
        return present[0]

    def hook(conn: sqlite3.Connection) -> None:
        for h in present:
            h(conn)

    return hook


def _marks(values: tuple[str, ...]) -> str:
    return ",".join("?" * len(values))


def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    item = dict(row)
    item["payload"] = json.loads(item.pop("payload_json") or "{}")
    raw = item.pop("submission_json")
    item["submission"] = json.loads(raw) if raw else None
    return item
