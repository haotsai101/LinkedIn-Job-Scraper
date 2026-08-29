"""Dump the live SQLite schema as Markdown — one table per section.

The generated document is the single source of truth for what columns actually
exist, replacing the hand-maintained (and drifted) ``DatabaseStructure.md``.

Usage:
    python scripts/dump_schema.py [path/to/linkedin_jobs.db] > DatabaseStructure.md
    python scripts/dump_schema.py --check [path/to/linkedin_jobs.db]

``--check`` exits non-zero if DatabaseStructure.md is stale relative to the DB
(useful in CI once a canonical DB is available).
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

DEFAULT_DB_PATH = "linkedin_jobs.db"
DOC_PATH = "DatabaseStructure.md"

_HEADER = """# Database Structure

<!-- GENERATED FILE — do not edit by hand.
     Regenerate with:  python scripts/dump_schema.py > DatabaseStructure.md
     Schema DDL for fresh databases lives in scripts/create_db.py;
     migrations that evolve an existing database live in scripts/migrations/. -->
"""


def _sql_identifier(name: str) -> str:
    """Quote an identifier for interpolation into a PRAGMA (no bind params allowed)."""
    return '"' + name.replace('"', '""') + '"'


def dump(db_path: str | Path = DEFAULT_DB_PATH) -> str:
    db_path = Path(db_path)
    if not db_path.exists():
        raise SystemExit(f"error: database not found: {db_path}")

    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        tables = [
            r[0]
            for r in cur.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' ORDER BY name"
            ).fetchall()
        ]

        parts = [_HEADER]
        for table in tables:
            parts.append(f"## {table}\n")
            parts.append("| Column | Type | Not Null | Default | PK |")
            parts.append("| --- | --- | --- | --- | --- |")
            # PRAGMA can't be parameterized; table name comes from sqlite_master.
            for _cid, col, ctype, notnull, dflt, pk in cur.execute(
                f"PRAGMA table_info({_sql_identifier(table)})"
            ).fetchall():
                parts.append(
                    f"| {col} | {ctype or ''} | {'yes' if notnull else ''} "
                    f"| {'' if dflt is None else dflt} | {pk or ''} |"
                )
            parts.append("")

            indexes = cur.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='index' "
                "AND tbl_name=? AND sql IS NOT NULL ORDER BY name",
                (table,),
            ).fetchall()
            if indexes:
                parts.append("Indexes:")
                for name, sql in indexes:
                    parts.append(f"- `{name}` — `{sql.strip()}`")
                parts.append("")

        return "\n".join(parts).rstrip() + "\n"
    finally:
        conn.close()


def main(argv: list[str]) -> int:
    check = "--check" in argv
    args = [a for a in argv if not a.startswith("--")]
    db_path = args[0] if args else DEFAULT_DB_PATH

    rendered = dump(db_path)

    if check:
        current = Path(DOC_PATH).read_text() if Path(DOC_PATH).exists() else ""
        if current.strip() != rendered.strip():
            print(f"error: {DOC_PATH} is out of date — regenerate with "
                  f"`python scripts/dump_schema.py > {DOC_PATH}`", file=sys.stderr)
            return 1
        print(f"{DOC_PATH} is up to date.")
        return 0

    sys.stdout.write(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
