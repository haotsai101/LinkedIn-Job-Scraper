from scripts.create_db import create_tables
from scripts.database_scripts import insert_job_postings
from scripts.fetch import JobSearchRetriever
import sqlite3
import time
from collections import deque
import pandas as pd


sleep_times = deque(maxlen=5)
first = True
sleep_factor = 3

conn = sqlite3.connect('linkedin_jobs.db')
cursor = conn.cursor()

create_tables(conn, cursor)


KEYWORDS = "software engineer AI ML"
TARGET = 100  # stop after this many new jobs are inserted; set to None to run forever

# Remote (workplaceType:2) and Utah (geoId:102095887) — run two passes alternating
SEARCH_CONFIGS = [
    ("remote", dict(filters="sortBy:List(DD),workplaceType:List(2)")),
    ("utah",   dict(filters="sortBy:List(DD)", geo_id="102095887")),
]
searchers = [(label, JobSearchRetriever(keywords=KEYWORDS, count=25, **kwargs)) for label, kwargs in SEARCH_CONFIGS]
pages = {label: 1 for label, _ in SEARCH_CONFIGS}

total_new = 0

while True:
    for label, job_searcher in searchers:
        if TARGET and total_new >= TARGET:
            break
        page = pages[label]
        all_results = job_searcher.get_jobs(page)

        query = "SELECT job_id FROM jobs WHERE job_id IN ({})".format(','.join(['?'] * len(all_results)))
        cursor.execute(query, list(all_results.keys()))
        result = [r[0] for r in cursor.fetchall()]
        new_results = {job_id: job_info for job_id, job_info in all_results.items() if job_id not in result}
        insert_job_postings(new_results, conn, cursor)
        total_non_sponsored = len([x for x in all_results.values() if x['sponsored'] is False])
        new_non_sponsored = len([x for x in new_results.values() if x['sponsored'] is False])
        total_new += len(new_results)
        print('[{}] {}/{} NEW | {}/{} NON-PROMOTED | page {} | total new: {}'.format(
            label, len(new_results), len(all_results), new_non_sponsored, total_non_sponsored, page, total_new))
        if not first:
            seconds_per_job = sleep_factor / max(len(new_results), 1)
            sleep_factor = min(seconds_per_job * total_non_sponsored * .75, 60)
        first = False
        pages[label] += 1

        print('Sleeping For {} Seconds...'.format(min(60, sleep_factor)))
        time.sleep(min(60, sleep_factor))
        print('Resuming...')

    if TARGET and total_new >= TARGET:
        print('Reached target of {} new jobs. Done.'.format(TARGET))
        break
