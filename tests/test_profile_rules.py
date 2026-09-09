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
