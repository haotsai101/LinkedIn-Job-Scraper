"""Config-validation tests for the Dagster ops in ``scripts/dagster_retrievers.py``.

Both ``search_jobs_op`` (T6) and ``fetch_job_details_op`` (T21) declare a
``config_schema`` while their schedules (``search_schedule`` /
``details_schedule``, both ``default_status=RUNNING``) supply **no** run_config.
If any field is required, every scheduled tick fails Dagster config validation
before doing any work. These tests pin the contract: the scheduled jobs must
build a valid run with an empty config, and explicit config must still override
the defaults.
"""

from dagster import validate_run_config

from scripts.dagster_retrievers import (
    fetch_details_only,
    search_and_fetch_jobs,
    search_jobs_only,
    unscraped_jobs_sensor,
)


def _details_config(resolved):
    return resolved["ops"]["fetch_job_details_op"]["config"]


# ── fetch_job_details_op / T21 ────────────────────────────────────────────────

def test_fetch_details_only_validates_with_no_config():
    """details_schedule supplies no run_config — this must not raise."""
    resolved = validate_run_config(fetch_details_only)
    cfg = _details_config(resolved)
    assert cfg["max_updates"] == 25
    assert cfg["sleep_time"] == 30


def test_fetch_details_only_explicit_config_overrides_defaults():
    resolved = validate_run_config(
        fetch_details_only,
        {"ops": {"fetch_job_details_op": {"config": {"max_updates": 7, "sleep_time": 1}}}},
    )
    cfg = _details_config(resolved)
    assert cfg["max_updates"] == 7
    assert cfg["sleep_time"] == 1


def test_fetch_details_only_partial_config_fills_remaining_default():
    resolved = validate_run_config(
        fetch_details_only,
        {"ops": {"fetch_job_details_op": {"config": {"max_updates": 50}}}},
    )
    cfg = _details_config(resolved)
    assert cfg["max_updates"] == 50
    assert cfg["sleep_time"] == 30


def test_unscraped_jobs_sensor_run_config_still_validates():
    """The sensor path passes explicit config; it must remain valid."""
    run_config = {
        "ops": {
            "fetch_job_details_op": {
                "config": {"max_updates": 50, "sleep_time": 30}
            }
        }
    }
    validate_run_config(fetch_details_only, run_config)
    # sanity: the sensor object is importable and wired to this job
    assert unscraped_jobs_sensor.job.name == fetch_details_only.name


# ── regression: search side (T6) and the combined job ─────────────────────────

def test_search_jobs_only_validates_with_no_config():
    validate_run_config(search_jobs_only)


def test_search_and_fetch_jobs_validates_with_no_config():
    """Combined unscheduled job — both ops must default cleanly."""
    validate_run_config(search_and_fetch_jobs)
