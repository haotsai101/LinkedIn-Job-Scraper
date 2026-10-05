"""answer_store.py — persistent answers to Easy Apply form questions (EA1).

Easy Apply forms ask the same questions over and over ("Are you legally
authorized to work in the US?", "How many years of Python experience do you
have?"). Each answer is stored in the ``form_answers`` table, keyed by the
normalized question text + field kind group + the set of options, so a question
is only ever put to the LLM once.

Lookup order used by ``linkedin_apply.EasyApplyFlow`` (see
``linkedin_apply._resolve_field_value``):

1. **this store** — rows whose ``source`` is ``manual`` (hand-edited, always
   wins) or ``llm`` (answered once by the Agent SDK, reused forever);
2. the deterministic profile rule table (``_get_profile_value``);
3. the Agent SDK (``_ask_llm``) — its answer is written back here.

Profile-rule answers are *also* recorded (``source='profile'``) so the table is
a complete picture of the questions seen, but they are never served from the
table: the rules re-resolve from ``user_profile.json`` every time, so editing
the profile can't leave a stale copy behind.

Answers to job-specific questions ("Why do you want to work at Acme?") are
saved for audit but flagged ``job_specific=1`` and never served for a different
job.

Precedence when writing: ``manual`` > ``llm`` > ``profile`` — a lower-ranked
writer never overwrites a higher-ranked row.
"""
from __future__ import annotations

import re
import sqlite3
import time

SOURCES = ("profile", "llm", "manual")
# Only these sources are served from the table (see module docstring).
SERVED_SOURCES = ("manual", "llm")

_CHOICE_KINDS = {"select", "select-one", "select-multiple", "radio"}
_LONG_KINDS = {"textarea", "contenteditable"}

# A question that names the employer / role can't be reused for another job.
_JOB_SPECIFIC_RE = re.compile(
    r"\b(this (company|role|position|job|team|opportunity|organization)"
    r"|our (company|team|mission|values|culture|product)"
    r"|why (do you want|are you interested|would you like)"
    r"|interested in (working|joining|this))\b"
)


def normalize_question(label: str) -> str:
    """Canonical form of a form label: lowercase, whitespace collapsed,
    required-markers and trailing punctuation stripped, doubled label text
    (LinkedIn often renders ``"X X"`` from nested spans) collapsed."""
    s = (label or "").lower().replace("_", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"\*?\s*required\s*$", "", s).strip()
    s = s.replace("*", "").replace("(required)", "")
    s = re.sub(r"\s+", " ", s).strip(" \t:?.!")
    half = len(s) // 2
    if len(s) > 3 and len(s) % 2 == 1 and s[:half] == s[half + 1:] and s[half] == " ":
        s = s[:half]
    return s


def kind_group(kind: str) -> str:
    """``choice`` (select/radio) | ``checkbox`` | ``long`` (textarea) | ``text``."""
    k = (kind or "text").lower()
    if k in _CHOICE_KINDS:
        return "choice"
    if k == "checkbox":
        return "checkbox"
    if k in _LONG_KINDS:
        return "long"
    return "text"


def options_key(options) -> str:
    """Order-insensitive, case-insensitive fingerprint of a field's options."""
    if not options:
        return ""
    cleaned = {re.sub(r"\s+", " ", str(o)).strip().lower() for o in options}
    return "|".join(sorted(c for c in cleaned if c))


def is_job_specific(label: str, kind: str = "text", company: str = "") -> bool:
    """Whether an answer to this question only makes sense for the current job."""
    norm = normalize_question(label)
    if company and len(company.strip()) > 2 and company.strip().lower() in norm:
        return True
    if _JOB_SPECIFIC_RE.search(norm):
        return True
    return False


class AnswerStore:
    """Thin wrapper over the ``form_answers`` table (one sqlite connection)."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn

    # ── read ────────────────────────────────────────────────────────────────
    def lookup(self, label: str, kind: str = "text", options=None) -> str | None:
        """Return a stored ``manual``/``llm`` answer for this question, or None.

        A choice answer that isn't one of the field's *current* options is
        treated as a miss (the form changed under us). Serving a row bumps its
        ``uses`` / ``last_used_at``.
        """
        q = normalize_question(label)
        if not q:
            return None
        row = self.conn.execute(
            "SELECT id, answer FROM form_answers "
            "WHERE question_key=? AND kind_group=? AND options_key=? "
            "AND job_specific=0 AND source IN ('manual','llm')",
            (q, kind_group(kind), options_key(options)),
        ).fetchone()
        if not row:
            return None
        row_id, answer = row
        if kind_group(kind) == "choice" and options:
            valid = {str(o).lower() for o in options} | {"decline"}
            if answer.lower() not in valid:
                return None
        now = int(time.time())
        self.conn.execute(
            "UPDATE form_answers SET uses = uses + 1, last_used_at = ? WHERE id = ?",
            (now, row_id),
        )
        self.conn.commit()
        return answer

    # ── write ───────────────────────────────────────────────────────────────
    def record(self, label: str, kind: str, options, answer: str, source: str,
               *, job_specific: bool = False) -> bool:
        """Upsert an answer. Empty answers are never stored. Returns True when a
        row was inserted or changed (False when nothing changed, e.g. a
        higher-ranked source already owns the row)."""
        if source not in SOURCES:
            raise ValueError(f"unknown answer source {source!r}")
        q = normalize_question(label)
        answer = (answer or "").strip()
        if not q or not answer:
            return False
        now = int(time.time())
        cur = self.conn.execute(
            """
            INSERT INTO form_answers
                (question_key, label, kind_group, options_key, answer, source,
                 job_specific, uses, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?, ?)
            ON CONFLICT(question_key, kind_group, options_key) DO UPDATE SET
                label = excluded.label,
                answer = excluded.answer,
                source = excluded.source,
                job_specific = excluded.job_specific,
                updated_at = excluded.updated_at
            WHERE (CASE excluded.source WHEN 'manual' THEN 3 WHEN 'llm' THEN 2 ELSE 1 END)
               >= (CASE form_answers.source WHEN 'manual' THEN 3 WHEN 'llm' THEN 2 ELSE 1 END)
              AND NOT (form_answers.source = excluded.source
                       AND form_answers.answer = excluded.answer
                       AND form_answers.job_specific = excluded.job_specific)
            """,
            (q, (label or "").strip(), kind_group(kind), options_key(options), answer,
             source, int(bool(job_specific)), now, now),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ── admin ───────────────────────────────────────────────────────────────
    def list_answers(self, source: str | None = None) -> list[tuple]:
        sql = ("SELECT id, source, kind_group, job_specific, uses, question_key, answer "
               "FROM form_answers")
        args: tuple = ()
        if source:
            sql += " WHERE source = ?"
            args = (source,)
        return self.conn.execute(sql + " ORDER BY source, uses DESC, id", args).fetchall()

    def forget(self, answer_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM form_answers WHERE id = ?", (answer_id,))
        self.conn.commit()
        return cur.rowcount > 0
