"""Unit tests for the T47 ``_get_profile_value`` rule table and the two T48
correctness fixes.

``tests/test_profile_value.py`` is the behaviour-preserving safety net for T47
(every assertion there must stay green with no change). This file adds:

* targeted unit tests for the non-trivial ``(matcher, resolver)`` entries and the
  small matcher/resolver factories the table is built from; and
* the T48 changes — the ``_anchored_in_bg`` hyphen/underscore boundary tightening
  and the leading-``"and"`` strip in string-form skills lists — which are the
  *only* intentional behaviour changes in this ticket.
"""

import linkedin_apply as la

_gpv = la._get_profile_value
_RULES = {r.name: r for r in la._PROFILE_VALUE_RULES}


# ── rule-table factories ──────────────────────────────────────────────────────

def test_kw_factory_is_a_substring_matcher():
    m = la._kw("foo bar", "baz")
    assert m("this has foo bar in it", "text", {}) is True
    assert m("baz here", "select", {}) is True
    assert m("nothing relevant", "text", {}) is False


def test_kw_kind_factory_requires_both_keyword_and_kind():
    m = la._kw_kind(("select", "radio"), "relocate")
    assert m("willing to relocate", "select", {}) is True
    assert m("willing to relocate", "radio", {}) is True
    # right keyword, wrong kind -> no match (falls through in the real table).
    assert m("willing to relocate", "text", {}) is False
    # right kind, no keyword -> no match.
    assert m("something else", "select", {}) is False


def test_const_and_pv_resolvers():
    assert la._const("Yes")("l", "k", {}) == "Yes"
    assert la._const(None)("l", "k", {}) is None
    assert la._pv("state")("l", "k", {"state": "Utah"}) == "Utah"
    assert la._pv("state")("l", "k", {}) is None
    # explicit default only kicks in when the key is absent.
    assert la._pv("country", "United States")("l", "k", {}) == "United States"
    assert la._pv("country", "United States")("l", "k", {"country": "Canada"}) == "Canada"


def test_edu_field_resolver_tolerates_a_non_dict_education():
    assert la._edu_field("school")("l", "k", {"education": {"school": "State U"}}) == "State U"
    assert la._edu_field("school")("l", "k", {}) is None
    assert la._edu_field("school")("l", "k", {"education": "B.S."}) is None


# ── individual rule matchers ─────────────────────────────────────────────────

def test_city_state_combined_matcher_needs_a_usable_location():
    rule = _RULES["city_state_combined"]
    with_loc = {"location": "Salt Lake City, Utah"}
    assert rule.matches("city, state", "text", with_loc) is True
    assert rule.resolve("city, state", "text", with_loc) == "Salt Lake City, Utah"
    # No location string -> matcher is False so iteration continues to the
    # individual city/state rules (T43 characterization test locks the outcome).
    assert rule.matches("city, state", "text", {"location": ""}) is False
    # "relocate to a city/state" is guarded out.
    assert rule.matches("relocate to this city and state", "text", with_loc) is False


def test_bare_years_experience_matcher_rejects_a_foreign_qualifier():
    rule = _RULES["bare_years_experience"]
    prof = {"current_title": "Software Engineer", "summary": "Backend engineer.",
            "years_experience": 6, "skills": "Python, SQL"}
    assert rule.matches("years of experience", "number", prof) is True
    assert rule.matches("years of professional experience", "text", prof) is True
    # "...as a Lead" is a role the applicant never held -> matcher False -> the
    # tiered rule downstream floors it.
    lead_q = "how many years of experience do you have as a lead?"
    assert rule.matches(lead_q, "text", prof) is False


def test_url_kind_fallback_only_matches_url_typed_fields():
    rule = _RULES["url_kind_fallback"]
    assert rule.matches("some other link", "url", {}) is True
    assert rule.matches("some other link", "text", {}) is False
    assert rule.resolve("x", "url", {}) == ""


def test_rule_names_are_unique_and_ordered_list_is_non_empty():
    names = [r.name for r in la._PROFILE_VALUE_RULES]
    assert len(names) == len(set(names))
    assert names[0] == "cover_letter_guard"       # unconditional None guard runs first
    assert names[-1] == "url_kind_fallback"        # catch-all runs last


# ── referral / "who referred you" fields must stay blank ──────────────────────

_APPLICANT = {"full_name": "Zhi-Hao Tsai", "preferred_name": ""}


def test_referral_name_field_stays_blank_not_own_name():
    # The broad "name" match would otherwise fill these with the applicant's own
    # name. A referral field must be left empty (skipped by the fill loop).
    for lbl in (
        "If you were referred by a Resource Innovations employee, please add their name here.",
        "Who referred you to this role?",
        "Name of the referring employee",
        "Referral",
        "Employee Referral - Referrer Name",
    ):
        assert _gpv(_APPLICANT, lbl, "text") == "", lbl


def test_referral_rule_precedes_full_name_rule():
    names = [r.name for r in la._PROFILE_VALUE_RULES]
    assert names.index("referral_name") < names.index("full_name")


def test_plain_name_fields_still_resolve_to_the_applicant():
    # regression guard — the new rule must not swallow ordinary name fields.
    assert _gpv(_APPLICANT, "Full name", "text") == "Zhi-Hao Tsai"
    assert _gpv(_APPLICANT, "Name", "text") == "Zhi-Hao Tsai"


def test_referral_source_is_a_how_did_you_hear_field_not_a_name_field():
    # "Referral source" / "how were you referred" ask HOW, not WHO — they must
    # resolve to the channel, never blank and never the applicant's name.
    assert _gpv(_APPLICANT, "Referral source", "text") == "LinkedIn"
    assert _gpv(_APPLICANT, "How were you referred to this role?", "text") == "LinkedIn"


# ── T48 #1: _anchored_in_bg boundary class includes "-" and "_" ───────────────

_GO_TO_PROFILE = {
    "current_title": "Software Engineer",
    "headline": "",
    "summary": "I was the go-to engineer on my team, building ai-driven tools.",
    "years_experience": 8,
    "skills": "Python, SQL",
}


def test_two_char_token_no_longer_matches_a_hyphen_fragment():
    # "go" appears only inside "go-to" (a conjunctive prefix, not the language),
    # and the applicant does not list Go -> the tenure must be floored, not kept.
    assert _gpv(_GO_TO_PROFILE, "Years of experience with Go", "text") == "1"
    # same for "ai" inside "ai-driven" (no standalone "ai", no AI skill listed).
    assert _gpv(_GO_TO_PROFILE, "Years of experience with AI", "text") == "1"


_ANCHOR_REGRESSION_PROFILE = {
    "current_title": "Software Engineer",
    "headline": "",
    "summary": "Expert in Go and Node.js. Also ship C#, C++ and plain C daily.",
    "years_experience": 8,
    "skills": "Python, Go, Node.js, C#, C++, C",
}


def test_anchoring_still_recognises_real_skill_tokens():
    # Regression guard for the T48 #1 boundary change: punctuation-bearing skill
    # names and the 2-char "go" token still anchor against real profile prose /
    # the skills list, so a listed skill keeps the full figure.
    for skill in ("Go", "Node.js", "C#", "C++", "C"):
        got = _gpv(_ANCHOR_REGRESSION_PROFILE, f"Years of experience with {skill}", "text")
        assert got == "8", f"{skill!r} -> {got!r}"
    # A genuinely foreign skill is still floored.
    assert _gpv(_ANCHOR_REGRESSION_PROFILE, "Years of experience with COBOL", "text") == "1"


# ── T48 #2: string-form skills split strips a leading "and" ───────────────────

def test_split_skill_string_strips_a_leading_conjunction():
    assert la._split_skill_string("Python, Go, and R") == ["Python", "Go", "R"]
    assert la._split_skill_string("Java; Kotlin; and Scala") == ["Java", "Kotlin", "Scala"]
    # no conjunction -> unchanged (aside from whitespace).
    assert la._split_skill_string("Python, SQL") == ["Python", "SQL"]


def test_years_of_skill_tier1_match_uses_the_tightened_boundary_class():
    # PR #65 review: _resolve_years_of_skill's Tier-1 regex is the same anchored
    # whole-token match as _anchored_in_bg and must share the "-"/"_" boundary
    # chars — otherwise a listed 2-char skill ("Go") matches a hyphen fragment in
    # the label ("a go-to methodology") and returns full tenure instead of "1".
    prof = {
        "current_title": "Software Engineer",
        "headline": "",
        "summary": "Backend engineer.",
        "years_experience": 8,
        "skills": "Go, Python",
    }
    hyphen_q = "years of experience with a go-to methodology"
    assert la._resolve_years_of_skill(hyphen_q, "text", prof) == "1"
    assert _gpv(prof, hyphen_q, "text") == "1"
    # positive control: a real "years of Go experience" question for the same
    # profile still Tier-1 matches the listed skill -> full tenure.
    real_q = "how many years of go experience do you have?"
    assert la._resolve_years_of_skill(real_q, "text", prof) == "8"
    assert _gpv(prof, real_q, "text") == "8"


def test_leading_and_in_skills_no_longer_hides_a_single_char_skill():
    # "...and R" used to land in skill_entries as "and r" != "r", so the exact
    # single-char skill check missed and "years of experience with R" was floored.
    prof = {
        "current_title": "Data Scientist",
        "summary": "Statistician and modeller.",
        "years_experience": 7,
        "skills": "Python, Go, and R",
    }
    assert _gpv(prof, "Years of experience with R", "text") == "7"
    # control: an unlisted single-char skill is still foreign -> floored.
    assert _gpv(prof, "Years of experience with C", "text") == "1"


# ── T50: work_history rules ─────────────────────────────────────────────────

_WORK_HISTORY_PROFILE = {
    "full_name": "Jordan Rivera",
    "current_title": "Software Engineer",
    "work_history": [
        {
            "employer": "Instructure",
            "title": "Software Engineer",
            "location": "American Fork, Utah",
            "start_date": "2022-09",
            "end_date": None,
            "current": True,
            "bullets": ["Built Go microservices on AWS EKS."],
        },
        {
            "employer": "Qualtrics",
            "title": "Software Engineer I",
            "location": "Provo, Utah",
            "start_date": "2021-01",
            "end_date": "2022-08",
            "current": False,
            "bullets": ["Developed FastAPI services."],
        },
    ],
}


def test_latest_work_history_entry_prefers_the_current_flag():
    # The current:True entry wins even if it isn't first in the list.
    out_of_order = {
        "work_history": [
            {"employer": "Old Co", "current": False},
            {"employer": "New Co", "current": True},
        ]
    }
    assert la._latest_work_history_entry(out_of_order)["employer"] == "New Co"
    # No current:True anywhere -> falls back to the first (most-recent-first) entry.
    assert la._latest_work_history_entry(_WORK_HISTORY_PROFILE)["employer"] == "Instructure"


def test_latest_work_history_entry_tolerates_missing_or_malformed_data():
    assert la._latest_work_history_entry({}) == {}
    assert la._latest_work_history_entry({"work_history": []}) == {}
    assert la._latest_work_history_entry({"work_history": "not a list"}) == {}
    assert la._latest_work_history_entry({"work_history": ["not a dict"]}) == {}


def test_current_company_falls_back_to_work_history_employer():
    # No legacy current_company/employer field -> resolves from work_history.
    assert _gpv(_WORK_HISTORY_PROFILE, "Current employer", "text") == "Instructure"
    assert _gpv(_WORK_HISTORY_PROFILE, "Most recent employer", "text") == "Instructure"
    assert _gpv(_WORK_HISTORY_PROFILE, "Company name", "text") == "Instructure"
    # No work_history and no legacy field at all -> "N/A" (unchanged pre-T50 behavior).
    assert _gpv({}, "Current employer", "text") == "N/A"


def test_current_company_legacy_field_still_wins_over_work_history():
    prof = dict(_WORK_HISTORY_PROFILE, current_company="Acme Corp")
    assert _gpv(prof, "Current employer", "text") == "Acme Corp"


def test_work_history_start_date_resolves_from_the_current_entry():
    assert _gpv(_WORK_HISTORY_PROFILE, "Start Date", "text") == "2022-09"
    assert _gpv(_WORK_HISTORY_PROFILE, "Employment Start Date", "text") == "2022-09"
    assert _gpv(_WORK_HISTORY_PROFILE, "From Date", "text") == "2022-09"
    # No work history at all -> None (no LLM-fabricated date).
    assert _gpv({}, "Start Date", "text") is None


def test_work_history_end_date_resolves_to_present_when_current():
    assert _gpv(_WORK_HISTORY_PROFILE, "End Date", "text") == "Present"
    assert _gpv(_WORK_HISTORY_PROFILE, "To Date", "text") == "Present"


def test_work_history_end_date_resolves_to_the_real_date_when_not_current():
    non_current_profile = {"work_history": [_WORK_HISTORY_PROFILE["work_history"][1]]}
    assert _gpv(non_current_profile, "End Date", "text") == "2022-08"


def test_job_offer_start_date_rule_is_unaffected_by_work_history_rules():
    # work_history_start_date matches "start date" by EXACT label equality, not
    # substring, precisely so it doesn't steal a qualified job-offer phrasing —
    # those still fall through to (and substring-match) the job-offer rule and
    # resolve to "Immediately", even when a work_history entry is present.
    assert _gpv(_WORK_HISTORY_PROFILE, "When can you start?", "select") == "Immediately"
    assert _gpv(_WORK_HISTORY_PROFILE, "Earliest available", "text") == "Immediately"
    assert _gpv(_WORK_HISTORY_PROFILE, "Desired Start Date", "text") == "Immediately"
    assert _gpv(_WORK_HISTORY_PROFILE, "Earliest Start Date", "text") == "Immediately"


def test_up_to_date_and_achievements_to_date_are_not_claimed_by_end_date_rule():
    # work_history_end_date matches "to date" by EXACT label equality too, so a
    # field that merely *contains* "to date" ("Is your profile up to date?",
    # "Summarize your achievements to date") is never misread as an
    # employment-record end date and correctly falls through to no match.
    assert _gpv(_WORK_HISTORY_PROFILE, "Is your profile up to date?", "text") is None
    assert _gpv(_WORK_HISTORY_PROFILE, "Summarize your achievements to date", "textarea") is None


def test_work_history_start_date_rule_precedes_the_job_offer_start_date_rule():
    names = [r.name for r in la._PROFILE_VALUE_RULES]
    assert names.index("work_history_start_date") < names.index("start_date")


def test_currently_work_here_checkbox():
    assert _gpv(_WORK_HISTORY_PROFILE, "I currently work here", "checkbox") == "on"
    non_current_profile = {"work_history": [_WORK_HISTORY_PROFILE["work_history"][1]]}
    # Not current -> "" (skip filling; the checkbox's native default is
    # unchecked, and _fill_field can only check a box, never uncheck one).
    assert _gpv(non_current_profile, "I currently work here", "checkbox") == ""


def test_currently_work_here_select():
    assert _gpv(_WORK_HISTORY_PROFILE, "Currently working in this role?", "select") == "Yes"
    non_current_profile = {"work_history": [_WORK_HISTORY_PROFILE["work_history"][1]]}
    assert _gpv(non_current_profile, "Currently working in this role?", "select") == "No"


def test_job_title_is_covered_by_the_existing_current_title_rule_not_duplicated():
    # T50 explicitly checked this before adding a new rule: "job title" already
    # routes through the pre-existing current_title rule. No separate
    # work-history-sourced "job title" rule was added.
    assert _gpv(_WORK_HISTORY_PROFILE, "Job Title", "text") == "Software Engineer"
    names = [r.name for r in la._PROFILE_VALUE_RULES]
    assert names.count("current_title") == 1
