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


KEYWORDS = "software data"  # Change this to any keyword you want to search for
job_searcher = JobSearchRetriever(keywords=KEYWORDS)
page = 1

while True:
    all_results = job_searcher.get_jobs(page)

    query = "SELECT job_id FROM jobs WHERE job_id IN ({})".format(','.join(['?'] * len(all_results)))
    cursor.execute(query, list(all_results.keys()))
    result = cursor.fetchall()
    result = [r[0] for r in result]
    new_results = {job_id: job_info for job_id, job_info in all_results.items() if job_id not in result}
    insert_job_postings(new_results, conn, cursor)
    total_non_sponsored = len([x for x in all_results.values() if x['sponsored'] is False])
    new_non_sponsored = len([x for x in new_results.values() if x['sponsored'] is False])
    print('{}/{} NEW RESULTS | {}/{} NEW NON-PROMOTED RESULTS on page: {}'.format(
        len(new_results), len(all_results), new_non_sponsored, total_non_sponsored, page))
    if not first:
        seconds_per_job = sleep_factor/max(len(new_results), 1)
        sleep_factor = min(seconds_per_job * total_non_sponsored * .75, 60)
    first = False
    page += 1
    if page % 10 == 0:
        page = 0  # reset to first page to catch any new jobs

    print('Sleeping For {} Seconds...'.format(min(60, sleep_factor)))
    time.sleep(min(60, sleep_factor))
    print('Resuming...')
