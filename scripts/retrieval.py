"""Shared retrieval loops for Phase 1 (discovery) and Phase 2 (enrichment).

Single source of truth for the core scrape loop. Both the standalone scripts
(``search_retriever.py`` / ``details_retriever.py``) and the Dagster ops
(``scripts/dagster_retrievers.py``) call these functions — the scripts are thin
wrappers that build a config, call one function, and exit (no ``while True``).

Neither function opens the database or refreshes sessions; the caller passes a
live ``sqlite3`` connection/cursor (after ``ensure_db_ready``) and the retriever
objects construct their own authenticated sessions as before.
"""

import random
import time

from scripts.database_scripts import insert_data, insert_job_postings
from scripts.fetch import JobDetailRetriever, JobSearchRetriever
from scripts.helpers import clean_job_postings
from scripts.search_config import SEARCH_KEYWORDS

# Remote (workplaceType:2) and Utah (geoId:102095887). One "round" fetches one
# page from each config, alternating — matches the standalone script's behaviour.
DEFAULT_SEARCH_CONFIGS = [
    ("remote", dict(filters="sortBy:List(DD),workplaceType:List(2)")),
    ("utah", dict(filters="sortBy:List(DD)", geo_id="102095887")),
]

# Defaults promoted from the module-level constants that used to live in the
# standalone scripts.
DEFAULT_TARGET = 100          # search: stop after this many new jobs (None = no cap)
DEFAULT_MAX_UPDATES = 25      # details: jobs enriched per batch
DEFAULT_SLEEP_TIME = 30       # details: seconds between batches
_SEARCH_BASE_SLEEP = 3        # search: starting value for the adaptive backoff


def run_search(
    conn,
    cursor,
    *,
    keywords: str = SEARCH_KEYWORDS,
    target: int | None = DEFAULT_TARGET,
    count: int = 25,
    search_configs=None,
    max_rounds: int | None = None,
    sleep_fn=time.sleep,
    log=print,
) -> dict:
    """Discover new job IDs and insert ``scraped=0`` rows.

    Args:
        conn / cursor: live SQLite handles (post ``ensure_db_ready``).
        keywords: Voyager ``keywords:`` query (defaults to the shared constant).
        target: stop once this many *new* jobs have been inserted. ``None``
            removes the cap (bounded then only by ``max_rounds`` / query
            exhaustion).
        count: results requested per page.
        search_configs: list of ``(label, kwargs)`` passed to
            ``JobSearchRetriever``; defaults to remote + Utah.
        max_rounds: hard cap on the number of rounds (one page per config each).
            ``None`` = run until ``target`` is hit or the query is exhausted
            (two consecutive rounds with zero new jobs).
        sleep_fn / log: injectable for tests.

    Returns:
        ``{"total_new_jobs", "total_new_non_sponsored", "pages_fetched"}``.
    """
    if search_configs is None:
        search_configs = DEFAULT_SEARCH_CONFIGS

    searchers = [
        (label, JobSearchRetriever(keywords=keywords, count=count, **kwargs))
        for label, kwargs in search_configs
    ]
    pages = {label: 1 for label, _ in search_configs}

    total_new = 0
    total_new_non_sponsored = 0
    sleep_factor = _SEARCH_BASE_SLEEP
    first = True
    rounds = 0
    empty_rounds = 0

    while True:
        if max_rounds is not None and rounds >= max_rounds:
            break
        rounds += 1
        round_new = 0

        for label, job_searcher in searchers:
            if target and total_new >= target:
                break

            page = pages[label]
            all_results = job_searcher.get_jobs(page)
            pages[label] += 1

            if not all_results:
                log(f"[{label}] page {page} — no results")
                continue

            query = "SELECT job_id FROM jobs WHERE job_id IN ({})".format(
                ",".join(["?"] * len(all_results))
            )
            cursor.execute(query, list(all_results.keys()))
            existing = {r[0] for r in cursor.fetchall()}
            new_results = {
                job_id: info
                for job_id, info in all_results.items()
                if job_id not in existing
            }
            insert_job_postings(new_results, conn, cursor)

            total_non_sponsored = len([x for x in all_results.values() if x["sponsored"] is False])
            new_non_sponsored = len([x for x in new_results.values() if x["sponsored"] is False])
            total_new += len(new_results)
            total_new_non_sponsored += new_non_sponsored
            round_new += len(new_results)

            log(
                f"[{label}] {len(new_results)}/{len(all_results)} NEW | "
                f"{new_non_sponsored}/{total_non_sponsored} NON-PROMOTED | "
                f"page {page} | total new: {total_new}"
            )

            # Adaptive backoff — identical formula to the old standalone loop.
            if not first:
                seconds_per_job = sleep_factor / max(len(new_results), 1)
                sleep_factor = min(seconds_per_job * total_non_sponsored * 0.75, 60)
            first = False

            nap = min(60, sleep_factor)
            log(f"Sleeping For {nap} Seconds...")
            sleep_fn(nap)
            log("Resuming...")

        if target and total_new >= target:
            log(f"Reached target of {target} new jobs. Done.")
            break

        # Query-exhaustion guard: without this a target that can never be met
        # (fewer than `target` matching jobs exist) would loop forever. One
        # transient empty round is tolerated; two in a row ends the run.
        empty_rounds = empty_rounds + 1 if round_new == 0 else 0
        if empty_rounds >= 2:
            log("No new jobs in two consecutive rounds — stopping.")
            break

    return {
        "total_new_jobs": total_new,
        "total_new_non_sponsored": total_new_non_sponsored,
        "pages_fetched": rounds,
    }


def run_detail_enrichment(
    conn,
    cursor,
    *,
    max_updates: int = DEFAULT_MAX_UPDATES,
    sleep_time: int = DEFAULT_SLEEP_TIME,
    retriever=None,
    sleep_fn=time.sleep,
    log=print,
) -> dict:
    """Enrich every ``scraped=0`` job, ``max_updates`` at a time.

    Args:
        conn / cursor: live SQLite handles (post ``ensure_db_ready``).
        max_updates: jobs enriched per batch.
        sleep_time: seconds to pause between batches (skipped after the last).
        retriever: a ``JobDetailRetriever``-like object; constructed if ``None``
            (deferred so tests can inject a fake without building sessions).
        sleep_fn / log: injectable for tests.

    Returns:
        ``{"updated_count", "remaining_jobs", "status"}`` — ``updated_count`` is
        cumulative across all batches this run.
    """
    if retriever is None:
        retriever = JobDetailRetriever()

    total_updated = 0

    while True:
        cursor.execute("SELECT job_id FROM jobs WHERE scraped = 0")
        pending = [r[0] for r in cursor.fetchall()]
        if not pending:
            log("All jobs scraped. Done.")
            break

        sample = random.sample(pending, min(max_updates, len(pending)))
        details = retriever.get_job_details(sample)
        details = clean_job_postings(details)
        insert_data(details, conn, cursor)
        total_updated += len(details)

        remaining = len(pending) - len(details)
        log(f"UPDATED {len(details)} VALUES IN DB — {remaining} remaining")

        if remaining <= 0:
            break

        log(f"Sleeping For {sleep_time} Seconds...")
        sleep_fn(sleep_time)
        log("Resuming...")

    cursor.execute("SELECT COUNT(*) FROM jobs WHERE scraped = 0")
    final_remaining = cursor.fetchone()[0]
    return {
        "updated_count": total_updated,
        "remaining_jobs": final_remaining,
        "status": "complete" if final_remaining == 0 else "in_progress",
    }
