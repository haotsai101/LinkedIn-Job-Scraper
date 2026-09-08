"""Phase 2 — Enrichment. Thin wrapper over ``scripts.retrieval.run_detail_enrichment``.

The batch loop itself lives in ``scripts/retrieval.py`` and is shared with the
Dagster ``fetch_job_details_op``. This script just parses args, opens the DB and
calls it once — no ``while True``.

    python details_retriever.py                    # enrich all scraped=0 jobs
    python details_retriever.py --max-updates 50
    python details_retriever.py --sleep 15
"""

import argparse
import sqlite3

from scripts.create_db import ensure_db_ready
from scripts.retrieval import DEFAULT_MAX_UPDATES, DEFAULT_SLEEP_TIME, run_detail_enrichment


def main():
    parser = argparse.ArgumentParser(
        description="Enrich scraped=0 jobs with full attributes."
    )
    parser.add_argument(
        "--max-updates", type=int, default=DEFAULT_MAX_UPDATES,
        help=f"Jobs to enrich per batch (default: {DEFAULT_MAX_UPDATES}).",
    )
    parser.add_argument(
        "--sleep", type=int, default=DEFAULT_SLEEP_TIME,
        help=f"Seconds to pause between batches (default: {DEFAULT_SLEEP_TIME}).",
    )
    parser.add_argument(
        "--database", default="linkedin_jobs.db", help="SQLite database path.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.database)
    cursor = conn.cursor()
    ensure_db_ready(conn, cursor)

    result = run_detail_enrichment(
        conn, cursor, max_updates=args.max_updates, sleep_time=args.sleep
    )

    conn.close()
    print(
        "Done — {updated_count} jobs enriched, {remaining_jobs} still pending "
        "({status}).".format(**result)
    )


if __name__ == "__main__":
    main()
