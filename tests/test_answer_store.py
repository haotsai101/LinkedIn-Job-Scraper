"""Tests for the Easy Apply answer store (EA1).

Covers ``answer_store`` (normalization, record/lookup, write precedence,
what is and isn't served), the ``linkedin_apply._resolve_field_value``
lookup order (store -> profile rules -> LLM, LLM answer written back), and
migration 004. No network, no browser, no real LLM — ``_ask_llm`` is faked.
"""
from __future__ import annotations

import asyncio
import importlib.util
import sqlite3
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import linkedin_apply  # noqa: E402
from answer_store import (  # noqa: E402
    AnswerStore,
    is_job_specific,
    kind_group,
    normalize_question,
    options_key,
)
from scripts.create_db import create_tables  # noqa: E402


@pytest.fixture
def store(tmp_path):
    conn = sqlite3.connect(str(tmp_path / "t.db"))
    create_tables(conn, conn.cursor())
    yield AnswerStore(conn)
    conn.close()


# ── normalization helpers ─────────────────────────────────────────────────────

def test_normalize_strips_required_and_punctuation():
    assert normalize_question("  Are you authorized to work in the US? * Required ") == \
        "are you authorized to work in the us"


def test_normalize_collapses_doubled_label():
    assert normalize_question("Years of Python Years of Python") == "years of python"


def test_normalize_keeps_genuinely_different_halves():
    assert normalize_question("City State") == "city state"


def test_kind_group_and_options_key():
    assert kind_group("select-one") == "choice" and kind_group("radio") == "choice"
    assert kind_group("textarea") == "long" and kind_group("checkbox") == "checkbox"
    assert kind_group("number") == "text" and kind_group("") == "text"
    assert options_key(["Yes", "No"]) == options_key(["no ", " YES"]) == "no|yes"
    assert options_key(None) == "" and options_key([]) == ""


def test_is_job_specific():
    assert is_job_specific("Why do you want to work here?")
    assert is_job_specific("What excites you about this role?")
    assert is_job_specific("Why Acme Corp?", company="Acme Corp")
    assert not is_job_specific("How many years of Python experience do you have?")
    assert not is_job_specific("Why Acme?", company="")


# ── store behaviour ───────────────────────────────────────────────────────────

def test_llm_answer_round_trips_and_counts_uses(store):
    assert store.lookup("Do you have a security clearance?", "radio", ["Yes", "No"]) is None
    assert store.record("Do you have a security clearance?", "radio", ["Yes", "No"], "No", "llm")
    # label cosmetics + option order don't matter
    assert store.lookup("do you have a security clearance? *", "radio", ["No", "Yes"]) == "No"
    assert store.lookup("Do you have a security clearance?", "radio", ["Yes", "No"]) == "No"
    (uses,) = store.conn.execute("SELECT uses FROM form_answers").fetchone()
    assert uses == 2


def test_same_label_different_options_or_kind_are_separate(store):
    store.record("Experience level", "select", ["Junior", "Senior"], "Senior", "llm")
    assert store.lookup("Experience level", "select", ["Junior", "Mid", "Senior"]) is None
    assert store.lookup("Experience level", "text", None) is None


def test_profile_rows_recorded_but_never_served(store):
    assert store.record("Email address", "email", None, "me@example.com", "profile")
    assert store.lookup("Email address", "email", None) is None
    assert store.list_answers("profile")[0][5] == "email address"


def test_job_specific_answers_saved_but_not_served(store):
    store.record("Why do you want to work here?", "textarea", None, "Because Acme.", "llm",
                 job_specific=True)
    assert store.lookup("Why do you want to work here?", "textarea", None) is None
    assert store.list_answers()[0][3] == 1


def test_choice_answer_must_still_be_a_current_option(store):
    store.record("Notice period", "select", ["1 week", "2 weeks"], "2 weeks", "llm")
    assert store.lookup("Notice period", "select", ["1 week", "2 weeks"]) == "2 weeks"
    store.conn.execute("UPDATE form_answers SET answer='3 weeks'")
    assert store.lookup("Notice period", "select", ["1 week", "2 weeks"]) is None


def test_decline_is_always_a_valid_choice_answer(store):
    opts = ["Yes", "No", "I prefer not to say"]
    store.record("Veteran status", "select", opts, "decline", "manual")
    assert store.lookup("Veteran status", "select", opts) == "decline"


def test_write_precedence_manual_over_llm_over_profile(store):
    args = ("Willing to relocate?", "radio", ["Yes", "No"])
    store.record(*args, "Yes", "profile")
    assert store.record(*args, "No", "llm")           # llm outranks profile
    assert not store.record(*args, "Yes", "profile")  # profile can't clobber llm
    assert store.record(*args, "Yes", "manual")       # manual outranks llm
    assert not store.record(*args, "No", "llm")       # llm can't clobber manual
    assert store.lookup(*args) == "Yes"
    assert len(store.list_answers()) == 1


def test_empty_answers_are_never_stored(store):
    assert not store.record("Anything else?", "text", None, "   ", "llm")
    assert store.list_answers() == []


def test_unknown_source_rejected(store):
    with pytest.raises(ValueError):
        store.record("q", "text", None, "a", "guess")


def test_forget_removes_the_row(store):
    store.record("Q one", "text", None, "A", "llm")
    rid = store.list_answers()[0][0]
    assert store.forget(rid) and not store.forget(rid)
    assert store.lookup("Q one", "text", None) is None


# ── resolver: store -> profile rules -> LLM -> write back ─────────────────────

PROFILE = {"email": "me@example.com", "full_name": "Test User"}


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def fake_llm(monkeypatch):
    calls = []

    async def _fake(model, profile, field):
        calls.append(field.get("label"))
        return "Blue"

    monkeypatch.setattr(linkedin_apply, "_ask_llm", _fake)
    return calls


def test_new_question_asks_llm_once_then_served_from_db(store, fake_llm):
    field = {"label": "What is your favourite colour?", "kind": "text", "options": []}
    resolve = linkedin_apply._resolve_field_value
    assert _run(resolve(store, PROFILE, field, "m")) == ("Blue", "llm")
    assert _run(resolve(store, PROFILE, field, "m")) == ("Blue", "db")
    assert fake_llm == ["What is your favourite colour?"]  # second call never hit the LLM


def test_profile_rule_beats_llm_and_is_logged_not_served(store, fake_llm):
    field = {"label": "Email address", "kind": "email", "options": []}
    resolve = linkedin_apply._resolve_field_value
    assert _run(resolve(store, PROFILE, field, "m")) == ("me@example.com", "profile")
    assert fake_llm == []
    # edit the profile -> the new value is used immediately (not a stale DB copy)
    new_profile = {**PROFILE, "email": "new@example.com"}
    assert _run(resolve(store, new_profile, field, "m")) == ("new@example.com", "profile")
    assert store.list_answers("profile")[0][6] == "new@example.com"


def test_manual_db_answer_overrides_profile_rule(store, fake_llm):
    store.record("Email address", "email", None, "override@example.com", "manual")
    field = {"label": "Email address", "kind": "email", "options": []}
    resolve = linkedin_apply._resolve_field_value
    assert _run(resolve(store, PROFILE, field, "m")) == ("override@example.com", "db")


def test_job_specific_llm_answer_is_not_reused(store, fake_llm):
    field = {"label": "Why do you want to work at Acme?", "kind": "textarea", "options": []}
    _run(linkedin_apply._resolve_field_value(store, PROFILE, field, "m", company="Acme"))
    _run(linkedin_apply._resolve_field_value(store, PROFILE, field, "m", company="Acme"))
    assert len(fake_llm) == 2


def test_store_none_matches_pre_ea1_behaviour(fake_llm):
    field = {"label": "What is your favourite colour?", "kind": "text", "options": []}
    assert _run(linkedin_apply._resolve_field_value(None, PROFILE, field, "m")) == ("Blue", "llm")
    assert _run(linkedin_apply._resolve_field_value(None, PROFILE, field, "m")) == ("Blue", "llm")
    assert len(fake_llm) == 2


def test_no_answer_returns_none(store, monkeypatch):
    async def _empty(model, profile, field):
        return None
    monkeypatch.setattr(linkedin_apply, "_ask_llm", _empty)
    field = {"label": "Zzz unknowable?", "kind": "text", "options": []}
    assert _run(linkedin_apply._resolve_field_value(store, PROFILE, field, "m")) == (None, None)
    assert store.list_answers() == []


# ── migration 004 ─────────────────────────────────────────────────────────────

def _load_migration():
    path = _REPO_ROOT / "scripts" / "migrations" / "004_form_answers.py"
    spec = importlib.util.spec_from_file_location("migration_004", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_migration_004_creates_table_and_is_idempotent(tmp_path):
    db = tmp_path / "old.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE jobs (job_id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()
    mod = _load_migration()
    mod.migrate(db)
    mod.migrate(db)  # second run is a no-op
    conn = sqlite3.connect(str(db))
    cols = {r[1] for r in conn.execute("PRAGMA table_info(form_answers)")}
    conn.close()
    assert {"question_key", "kind_group", "options_key", "answer", "source",
            "job_specific", "uses"} <= cols
