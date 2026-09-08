"""T17 — the shared retrieval loops in ``scripts/retrieval.py``.

No network: ``JobSearchRetriever`` is monkeypatched and ``JobDetailRetriever``
is injected as a fake. These pin that each loop processes a batch and
*terminates* (search stops at the target / when the query is exhausted; details
stops when no ``scraped=0`` rows remain) — the whole point of T17 replacing the
standalone ``while True`` bodies.
"""

import itertools
import sqlite3

import pytest

import scripts.retrieval as retrieval
from scripts.create_db import create_tables

_ID = itertools.count(100_000)


@pytest.fixture()
def db(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    create_tables(conn, conn.cursor())
    yield conn
    conn.close()


def _pending(conn):
    return conn.execute("SELECT COUNT(*) FROM jobs WHERE scraped = 0").fetchone()[0]


# ── search ───────────────────────────────────────────────────────────────────

class FakeSearcher:
    """Yields ``per_page`` brand-new job ids on every ``get_jobs`` call."""

    per_page = 30

    def __init__(self, *, keywords, count, **kwargs):
        self.kwargs = kwargs
        self.pages_seen = []

    def get_jobs(self, page):
        self.pages_seen.append(page)
        return {
            next(_ID): {"sponsored": False, "title": f"job {page}"}
            for _ in range(self.per_page)
        }


class EmptySearcher(FakeSearcher):
    def get_jobs(self, page):
        self.pages_seen.append(page)
        return {}


def test_run_search_stops_at_target(db, monkeypatch):
    monkeypatch.setattr(retrieval, "JobSearchRetriever", FakeSearcher)

    result = retrieval.run_search(
        db, db.cursor(), target=100, sleep_fn=lambda *_: None, log=lambda *_: None
    )

    # 2 configs * 30 new/page = 60 per round -> round 2 crosses 100, then stop.
    assert result["pages_fetched"] == 2
    assert result["total_new_jobs"] == 120
    assert result["total_new_non_sponsored"] == 120
    assert _pending(db) == 120  # every inserted row starts scraped=0


def test_run_search_respects_max_rounds(db, monkeypatch):
    monkeypatch.setattr(retrieval, "JobSearchRetriever", FakeSearcher)

    result = retrieval.run_search(
        db, db.cursor(), target=None, max_rounds=3,
        sleep_fn=lambda *_: None, log=lambda *_: None,
    )

    assert result["pages_fetched"] == 3
    assert result["total_new_jobs"] == 180


def test_run_search_terminates_when_query_exhausted(db, monkeypatch):
    """target set but unreachable (searcher returns nothing) -> must not loop
    forever; two empty rounds end the run."""
    monkeypatch.setattr(retrieval, "JobSearchRetriever", EmptySearcher)

    result = retrieval.run_search(
        db, db.cursor(), target=100, sleep_fn=lambda *_: None, log=lambda *_: None
    )

    assert result["total_new_jobs"] == 0
    assert result["pages_fetched"] == 2  # stopped after 2 consecutive empty rounds


def test_run_search_skips_already_known_ids(db, monkeypatch):
    monkeypatch.setattr(retrieval, "JobSearchRetriever", FakeSearcher)
    db.execute("INSERT INTO jobs (job_id, scraped) VALUES (?, 0)", (999_999,))
    db.commit()

    class OverlapSearcher(FakeSearcher):
        def get_jobs(self, page):
            self.pages_seen.append(page)
            return {
                999_999: {"sponsored": False, "title": "dup"},
                next(_ID): {"sponsored": True, "title": "new-promoted"},
            }

    monkeypatch.setattr(retrieval, "JobSearchRetriever", OverlapSearcher)
    result = retrieval.run_search(
        db, db.cursor(), target=1, sleep_fn=lambda *_: None, log=lambda *_: None
    )
    # the duplicate id is not re-counted; the one genuinely-new row per config is.
    assert result["total_new_jobs"] >= 1
    assert result["total_new_non_sponsored"] == 0  # only the promoted job was new


# ── details ──────────────────────────────────────────────────────────────────

class FakeDetailRetriever:
    """Returns the sentinel -1 for every id (clean_job_postings -> {'error': -1}
    -> insert_data sets scraped=-1), so the pending set shrinks each batch."""

    def __init__(self):
        self.batches = []

    def get_job_details(self, job_ids):
        ids = list(job_ids)
        self.batches.append(ids)
        return {jid: -1 for jid in ids}


def _seed_jobs(conn, n):
    conn.executemany(
        "INSERT INTO jobs (job_id, scraped) VALUES (?, 0)", [(i,) for i in range(1, n + 1)]
    )
    conn.commit()


def test_run_detail_enrichment_processes_all_in_batches(db):
    _seed_jobs(db, 60)
    fake = FakeDetailRetriever()

    result = retrieval.run_detail_enrichment(
        db, db.cursor(), max_updates=25, sleep_time=99,
        retriever=fake, sleep_fn=lambda *_: None, log=lambda *_: None,
    )

    assert [len(b) for b in fake.batches] == [25, 25, 10]
    assert result["updated_count"] == 60
    assert result["remaining_jobs"] == 0
    assert result["status"] == "complete"
    assert _pending(db) == 0


def test_run_detail_enrichment_noop_when_nothing_pending(db):
    fake = FakeDetailRetriever()
    result = retrieval.run_detail_enrichment(
        db, db.cursor(), retriever=fake, sleep_fn=lambda *_: None, log=lambda *_: None
    )
    assert fake.batches == []
    assert result == {"updated_count": 0, "remaining_jobs": 0, "status": "complete"}


def test_run_detail_enrichment_no_sleep_after_final_batch(db):
    _seed_jobs(db, 10)
    naps = []
    retrieval.run_detail_enrichment(
        db, db.cursor(), max_updates=25, sleep_time=30,
        retriever=FakeDetailRetriever(), sleep_fn=lambda s: naps.append(s),
        log=lambda *_: None,
    )
    assert naps == []  # single batch cleared everything -> never slept


# ── search adaptive-backoff formula ──────────────────────────────────────────
#
# The riskiest part of the T17 extraction: the per-page sleep in run_search is
# NOT a fixed value, it carries state (`sleep_factor`, the `first` flag) across
# iterations. These pin the exact formula against known inputs.
#
#   iteration 1 (first=True): no formula, nap = min(60, _SEARCH_BASE_SLEEP=3)
#   iteration N>1:            per_job = sleep_factor / max(len(new), 1)
#                             sleep_factor = min(per_job * non_sponsored * 0.75, 60)
#                             nap = min(60, sleep_factor)

def _batch(n_total, n_sponsored, first_id):
    """A get_jobs() return value: n_total cards, n_sponsored of them promoted."""
    return {
        first_id + i: {"sponsored": i < n_sponsored, "title": "x"}
        for i in range(n_total)
    }


def _install_scripted_searcher(monkeypatch, batches):
    queue = list(batches)

    class _Scripted:
        def __init__(self, *, keywords, count, **kwargs):
            pass

        def get_jobs(self, page):
            return queue.pop(0) if queue else {}

    monkeypatch.setattr(retrieval, "JobSearchRetriever", _Scripted)


def test_run_search_adaptive_backoff_formula(db, monkeypatch):
    # one search config -> one get_jobs() per round, so the nap sequence is
    # exactly [round1, round2].
    _install_scripted_searcher(monkeypatch, [
        _batch(4, 0, 500_000),    # round 1: 4 new, 4 non-promoted
        _batch(10, 2, 600_000),   # round 2: 10 new, 8 non-promoted
    ])
    naps = []

    retrieval.run_search(
        db, db.cursor(),
        target=None, max_rounds=2,
        search_configs=[("solo", {})],
        sleep_fn=lambda s: naps.append(s),
        log=lambda *_: None,
    )

    # round 1: first iteration -> base sleep, untouched by the formula
    assert naps[0] == retrieval._SEARCH_BASE_SLEEP == 3
    # round 2: seconds_per_job = 3 / 10 = 0.3 ; 0.3 * 8 * 0.75 = 1.8
    assert naps[1] == pytest.approx(1.8)


def test_run_search_backoff_is_capped_at_60(db, monkeypatch):
    # 99 of the round-2 ids already exist -> only 1 is "new" (tiny divisor) but
    # total_non_sponsored is 100 -> raw factor 3/1 * 100 * 0.75 = 225, clamped.
    db.executemany(
        "INSERT INTO jobs (job_id, scraped) VALUES (?, 0)",
        [(700_000 + i,) for i in range(99)],
    )
    db.commit()
    _install_scripted_searcher(monkeypatch, [
        _batch(3, 0, 500_000),          # round 1
        _batch(100, 0, 700_000),        # round 2: ids 700_000..700_099
    ])
    naps = []

    retrieval.run_search(
        db, db.cursor(),
        target=None, max_rounds=2,
        search_configs=[("solo", {})],
        sleep_fn=lambda s: naps.append(s),
        log=lambda *_: None,
    )

    assert naps[1] == 60
    assert all(n <= 60 for n in naps)
