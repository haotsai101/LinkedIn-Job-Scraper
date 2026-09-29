"""Persistence for OffsiteApply agent attempts (``offsite_applications``, OA1).

One row per attempt; the latest row per ``job_id`` is the current one.
``jobs.applied`` is **not** touched here — the session layer (OA11) sets it
through the existing ``mark_job`` path. Passwords are never stored: the only
account fields are ``account_email`` and ``account_host``.
"""
from __future__ import annotations

import json
import sqlite3
import time
from typing import Any

# Status values — design §3 / §7.
PREPARING = "PREPARING"
READY_FOR_REVIEW = "READY_FOR_REVIEW"
SUBMITTED = "SUBMITTED"
REJECTED = "REJECTED"
BLOCKED = "BLOCKED"
DEFERRED = "DEFERRED"
FAILED = "FAILED"
STATUSES = frozenset(
    {PREPARING, READY_FOR_REVIEW, SUBMITTED, REJECTED, BLOCKED, DEFERRED, FAILED}
)

# Columns update_attempt may set. id / job_id / created_at are fixed at insert.
_UPDATABLE = frozenset(
    {
        "status",
        "ats_host",
        "model_used",
        "fallback_reason",
        "answers_json",
        "tool_calls",
        "account_email",
        "account_host",
        "confirmation",
        "error",
        "submitted_at",
    }
)


def start_attempt(conn: sqlite3.Connection, job_id: int, ats_host: str | None) -> int:
    """Insert a ``PREPARING`` attempt for ``job_id`` and return its id."""
    now = int(time.time())
    cur = conn.execute(
        "INSERT INTO offsite_applications "
        "(job_id, status, ats_host, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (job_id, PREPARING, ats_host, now, now),
    )
    conn.commit()
    return int(cur.lastrowid)


def update_attempt(conn: sqlite3.Connection, attempt_id: int, **fields: Any) -> None:
    """Set ``fields`` on an attempt and bump ``updated_at``.

    ``answers`` (a list of dicts / pydantic models) is accepted as a convenience
    and stored as ``answers_json``. Unknown columns and unknown statuses raise
    ``ValueError``; ``password``-like keys are rejected outright.
    """
    if "answers" in fields:
        answers = fields.pop("answers")
        fields["answers_json"] = json.dumps(
            [a.model_dump() if hasattr(a, "model_dump") else a for a in answers]
        )
    bad = set(fields) - _UPDATABLE
    if bad:
        raise ValueError(f"unknown offsite_applications column(s): {sorted(bad)}")
    if "status" in fields and fields["status"] not in STATUSES:
        raise ValueError(f"unknown status: {fields['status']!r}")
    if not fields:
        return
    fields["updated_at"] = int(time.time())
    cols = ", ".join(f"{k} = ?" for k in fields)  # keys validated against _UPDATABLE
    cur = conn.execute(
        f"UPDATE offsite_applications SET {cols} WHERE id = ?",
        (*fields.values(), attempt_id),
    )
    if cur.rowcount == 0:
        raise ValueError(f"no offsite_applications row with id {attempt_id}")
    conn.commit()


def latest_attempt(conn: sqlite3.Connection, job_id: int) -> dict[str, Any] | None:
    """The most recent attempt for ``job_id`` as a dict (``answers`` decoded), or ``None``."""
    cur = conn.execute(
        "SELECT * FROM offsite_applications WHERE job_id = ? ORDER BY id DESC LIMIT 1",
        (job_id,),
    )
    row = cur.fetchone()
    if row is None:
        return None
    out = dict(zip([d[0] for d in cur.description], row, strict=True))
    out["answers"] = json.loads(out["answers_json"]) if out["answers_json"] else []
    return out


def status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Count of *current* attempts (latest per job) by status — for ``--stats``."""
    rows = conn.execute(
        "SELECT status, COUNT(*) FROM offsite_applications o "
        "WHERE id = (SELECT MAX(id) FROM offsite_applications WHERE job_id = o.job_id) "
        "GROUP BY status"
    ).fetchall()
    return dict(rows)
