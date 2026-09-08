"""Phase 1 — Discovery. Thin wrapper over ``scripts.retrieval.run_search``.

The retrieval loop itself lives in ``scripts/retrieval.py`` and is shared with
the Dagster ``search_jobs_op``. This script just parses args, opens the DB and
calls it once — no ``while True``.

    python search_retriever.py                 # stop at 100 new jobs (default)
    python search_retriever.py --target 250
    python search_retriever.py --target 0      # no cap (bounded by --max-rounds
                                               # or query exhaustion)
    python search_retriever.py --max-rounds 5  # at most 5 rounds
"""

import argparse
import sqlite3

from scripts.create_db import ensure_db_ready
from scripts.retrieval import DEFAULT_TARGET, run_search


def main():
    parser = argparse.ArgumentParser(description="Discover new LinkedIn job IDs.")
    parser.add_argument(
        "--target", type=int, default=DEFAULT_TARGET,
        help=f"Stop after this many new jobs are inserted (default: {DEFAULT_TARGET}; "
             "0 = no cap).",
    )
    parser.add_argument(
        "--max-rounds", type=int, default=None,
        help="Hard cap on rounds (one page per search config each). Default: none — "
             "run until --target is hit or the query is exhausted.",
    )
    parser.add_argument(
        "--database", default="linkedin_jobs.db", help="SQLite database path.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.database)
    cursor = conn.cursor()
    ensure_db_ready(conn, cursor)

    target = args.target if args.target and args.target > 0 else None
    result = run_search(conn, cursor, target=target, max_rounds=args.max_rounds)

    conn.close()
    print(
        "Done — {total_new_jobs} new jobs ({total_new_non_sponsored} non-promoted) "
        "over {pages_fetched} rounds.".format(**result)
    )


if __name__ == "__main__":
    main()
