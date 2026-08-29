"""Characterization tests for ``linkedin_apply._get_profile_value`` and ``_degree_rank``.

These lock down the *current* behavior of the deterministic form-field matcher so that
the upcoming ``common.py`` extraction (T4) and the Agent SDK migration (T14) can refactor
the surrounding module without silently changing what values get typed into job
application forms.

They are deliberately *characterization* tests: every assertion records what the function
returns today, not what it arguably "should" return. If a change to
``_get_profile_value`` makes one of these fail, that is a prompt to consciously decide
whether the behavior change is intended and to update the expectation here.

No network and no browser: the test only imports ``linkedin_apply`` and calls two pure
functions. The module's ``openai`` / ``playwright`` imports are import-guarded so this
works even when those packages are absent.
"""

import linkedin_apply as la

_gpv = la._get_profile_value


# A synthetic profile shaped like ``user_profile.json`` (see PROFILE_QUESTIONS in
# apply_jobs.py and the ``p.get(...)`` reads in linkedin_apply.py). All values are fake --
# no real PII.
PROFILE = {
    "full_name": "Jane Alexandra Doe",
    "preferred_name": "",
    "email": "jane@example.com",
    "phone": "+1-555-0100",
    "location": "Salt Lake City, Utah",
    "city": "Provo",
    "state": "Utah",
    "zip_code": "84101",
    "street_address": "123 Example St",
    "country": "United States",
    "linkedin_url": "https://linkedin.com/in/janedoe",
    "github_url": "https://github.com/janedoe",
    "portfolio_url": "https://janedoe.example.com",
    "website_url": "",
    "current_title": "Senior Software Engineer",
    "current_company": "Acme Corp",
    "headline": "Senior SWE | Backend & AI",
    "summary": "Senior engineer with a decade of backend experience.",
    "cover_letter_text": "I am excited about this role.",
    "years_experience": 10,
    "skills": "Python, SQL, ML",
    "education": {
        "degree": "M.S.",
        "field": "Computer Science",
        "school": "State University",
        "year": "2015",
    },
    "work_authorization": "US Citizen",
    "work_authorization_expiry": "N/A",
    "need_sponsorship": "no",
    "willing_to_relocate": "no",
    "willing_to_travel": "No",
    "preferred_salary": "150000 - 180000",
    "notice_period": "Immediately",
    "preferred_language": "English",
    "gender": "decline",
    "race": "decline",
    "veteran_status": "No",
    "disability_status": "No",
    # Absolute so os.path.abspath() is a no-op and the assertion is host-independent.
    "resume_path": "/home/jane/resume.pdf",
}


# ── identity ──────────────────────────────────────────────────────────────────

def test_identity_fields():
    assert _gpv(PROFILE, "First name") == "Jane"
    assert _gpv(PROFILE, "Given name") == "Jane"
    assert _gpv(PROFILE, "Last name") == "Doe"
    assert _gpv(PROFILE, "Surname") == "Doe"
    assert _gpv(PROFILE, "Full name") == "Jane Alexandra Doe"
    assert _gpv(PROFILE, "Name") == "Jane Alexandra Doe"
    assert _gpv(PROFILE, "Email address") == "jane@example.com"
    assert _gpv(PROFILE, "Phone number") == "+1-555-0100"
    assert _gpv(PROFILE, "Mobile") == "+1-555-0100"
    # "* Required" / newline noise in the label is stripped before matching.
    assert _gpv(PROFILE, "First name *\nRequired") == "Jane"
    # Middle name is intentionally blanked (user has none), not None.
    assert _gpv(PROFILE, "Middle name") == ""


def test_preferred_name_vs_first_name():
    p = {"full_name": "Jane Alexandra Doe", "preferred_name": "Janie"}
    # "preferred name" / "nickname" use the preferred_name value...
    assert _gpv(p, "Preferred name") == "Janie"
    assert _gpv(p, "Nickname") == "Janie"
    # ...but "preferred FIRST name" hits the earlier "first name" branch instead.
    assert _gpv(p, "Preferred first name") == "Jane"
    # Empty preferred_name falls back to the first token of full_name.
    assert _gpv(PROFILE, "Preferred name") == "Jane"


# ── numbers ───────────────────────────────────────────────────────────────────

def test_numbers_experience_and_salary():
    assert _gpv(PROFILE, "Years of experience", "number") == "10"
    assert _gpv(PROFILE, "How many years of experience do you have?", "number") == "10"
    # Salary stored as a range -> upper bound is returned for single-value fields.
    assert _gpv(PROFILE, "Desired salary", "text") == "180000"
    assert _gpv(PROFILE, "Expected compensation", "text") == "180000"
    # Plain single-value salary is returned verbatim.
    assert _gpv({"preferred_salary": "150000"}, "Desired salary", "text") == "150000"


# ── work authorization / visa / citizenship ───────────────────────────────────

def test_work_authorization_and_visa():
    assert _gpv(PROFILE, "Are you legally authorized to work in the United States?", "select") == "Yes"
    assert _gpv(PROFILE, "Are you eligible to work in the United States?", "select") == "Yes"
    # "right to work" hits the residency-country picker branch first -> returns the country.
    assert _gpv(PROFILE, "Do you have the right to work in one of the following countries?", "select") == "United States"
    # US citizen -> not OPT/H1B/etc -> sponsorship "No".
    assert _gpv(PROFILE, "Will you require visa sponsorship?", "select") == "No"
    assert _gpv(PROFILE, "Do you require a work visa?", "select") == "No"
    # OPT/STEM question with a US-citizen profile -> "No".
    assert _gpv(PROFILE, "Are you currently on OPT or STEM OPT?", "select") == "No"
    # US-based residency question -> "Yes".
    assert _gpv(PROFILE, "Are you currently based in the United States?", "select") == "Yes"
    # There is no dedicated citizenship branch -> falls through to None.
    assert _gpv(PROFILE, "Are you a US citizen?", "select") is None
    # F-1 / student visa -> "No".
    assert _gpv(PROFILE, "Are you on an F-1 visa?", "select") == "No"


def test_sponsorship_when_profile_needs_it():
    p = {"need_sponsorship": "yes", "work_authorization": "H1B"}
    assert _gpv(p, "Will you now or in the future require sponsorship?", "select") == "Yes"


# ── EEO / demographic ─────────────────────────────────────────────────────────

def test_eeo_demographic_fields():
    assert _gpv(PROFILE, "What is your gender?", "select") == "decline"
    assert _gpv(PROFILE, "Race / Ethnicity", "select") == "decline"
    assert _gpv(PROFILE, "Are you a protected veteran?", "select") == "No"
    assert _gpv(PROFILE, "Do you have a disability?", "select") == "No"
    # With the keys absent, gender/race default to "decline"; veteran/disability to "No".
    assert _gpv({}, "Gender", "select") == "decline"
    assert _gpv({}, "Ethnicity", "select") == "decline"
    assert _gpv({}, "Protected veteran status", "select") == "No"
    assert _gpv({}, "Disability status", "select") == "No"


# ── education ─────────────────────────────────────────────────────────────────

def test_education_fields():
    assert _gpv(PROFILE, "What school did you attend?", "text") == "State University"
    assert _gpv(PROFILE, "University", "text") == "State University"
    assert _gpv(PROFILE, "Field of study", "text") == "Computer Science"
    assert _gpv(PROFILE, "Major", "text") == "Computer Science"
    assert _gpv(PROFILE, "Degree", "text") == "M.S."
    # "Highest level of education" maps the abbreviation to a display string.
    assert _gpv(PROFILE, "Highest level of education", "select") == "Master's Degree"
    # As a radio it becomes a Yes/No "do you have a degree" question.
    assert _gpv(PROFILE, "Highest level of education completed", "radio") == "Yes"
    # "In which year did you complete your degree?" -> graduation year.
    assert _gpv(PROFILE, "In which year did you complete your degree?", "text") == "2015"


def test_specific_degree_completion_uses_degree_rank():
    # User holds an M.S. (rank 3).
    assert _gpv(PROFILE, "Have you completed a Bachelor's degree?", "select") == "Yes"
    assert _gpv(PROFILE, "Have you completed a Master's degree?", "select") == "Yes"
    assert _gpv(PROFILE, "Have you completed a Doctorate?", "select") == "No"
    assert _gpv(PROFILE, "Have you completed a PhD?", "select") == "No"


def test_degree_rank():
    assert la._degree_rank("") == 0
    assert la._degree_rank("nonsense") == 0
    assert la._degree_rank("Associate of Arts") == 1
    assert la._degree_rank("B.S.") == 2
    assert la._degree_rank("Bachelor of Science") == 2
    assert la._degree_rank("M.S.") == 3
    assert la._degree_rank("MBA") == 3
    assert la._degree_rank("Master of Arts") == 3
    assert la._degree_rank("Ph.D.") == 4
    assert la._degree_rank("Doctorate") == 4
    # Rank ordering is what callers rely on.
    assert la._degree_rank("PhD") > la._degree_rank("M.S.") > la._degree_rank("B.S.")


# ── links ─────────────────────────────────────────────────────────────────────

def test_link_fields():
    assert _gpv(PROFILE, "LinkedIn URL", "text") == "https://linkedin.com/in/janedoe"
    assert _gpv(PROFILE, "LinkedIn profile", "url") == "https://linkedin.com/in/janedoe"
    assert _gpv(PROFILE, "GitHub URL", "text") == "https://github.com/janedoe"
    assert _gpv(PROFILE, "Portfolio", "text") == "https://janedoe.example.com"
    assert _gpv(PROFILE, "Personal website", "text") == "https://janedoe.example.com"
    # "website" (non-personal) falls back through website_url -> portfolio_url.
    assert _gpv(PROFILE, "Website", "text") == "https://janedoe.example.com"
    # Unknown social platforms -> "" so the LLM cannot fabricate a handle.
    assert _gpv(PROFILE, "Twitter", "url") == ""
    assert _gpv(PROFILE, "Instagram profile", "text") == ""
    # Any other url-typed field we didn't match -> "".
    assert _gpv(PROFILE, "Some other link", "url") == ""


# ── address ───────────────────────────────────────────────────────────────────

def test_address_fields():
    assert _gpv(PROFILE, "Which state do you live in?", "text") == "Utah"
    assert _gpv(PROFILE, "State / Province", "text") == "Utah"
    assert _gpv(PROFILE, "Zip code", "text") == "84101"
    assert _gpv(PROFILE, "Postal code", "text") == "84101"
    assert _gpv(PROFILE, "Street address", "text") == "123 Example St"
    # Address line 2 is intentionally blanked.
    assert _gpv(PROFILE, "Address line 2", "text") == ""
    assert _gpv(PROFILE, "Country", "text") == "United States"
    assert _gpv(PROFILE, "What country are you located in?", "text") == "United States"
    assert _gpv(PROFILE, "Phone country code", "text") == "United States"
    # "city"/"location" questions return the location string.
    assert _gpv(PROFILE, "Current location", "text") == "Salt Lake City, Utah"
    assert _gpv(PROFILE, "City", "text") == "Salt Lake City, Utah"


# ── cover letter / long-text ──────────────────────────────────────────────────

def test_cover_letter_and_longtext_are_declined():
    # Cover letter fields never return a value (no resume path, no LLM text here).
    assert _gpv(PROFILE, "Cover letter", "file") is None
    assert _gpv(PROFILE, "Cover letter", "textarea") is None
    assert _gpv(PROFILE, "Covering letter", "text") is None
    # Open-ended "why do you want to work here" style prompts are left to the LLM -> None.
    assert _gpv(PROFILE, "Why do you want to work here?", "textarea") is None
    assert _gpv(
        PROFILE,
        "Please describe in detail your experience building distributed systems",
        "textarea",
    ) is None


# ── resume upload ─────────────────────────────────────────────────────────────

def test_resume_upload():
    assert _gpv(PROFILE, "Upload your resume", "file") == "/home/jane/resume.pdf"
    assert _gpv(PROFILE, "Attach CV", "file") == "/home/jane/resume.pdf"
    # No resume_path -> None.
    assert _gpv({}, "Upload your resume", "file") is None


# ── misc known branches ───────────────────────────────────────────────────────

def test_misc_known_branches():
    assert _gpv(PROFILE, "Current company", "text") == "Acme Corp"
    assert _gpv(PROFILE, "Current title", "text") == "Senior Software Engineer"
    assert _gpv(PROFILE, "Professional headline", "text") == "Senior SWE | Backend & AI"
    assert _gpv(PROFILE, "Professional summary", "textarea") == (
        "Senior engineer with a decade of backend experience."
    )
    assert _gpv(PROFILE, "How did you hear about us?", "text") == "LinkedIn"
    assert _gpv(PROFILE, "When can you start?", "select") == "Immediately"
    assert _gpv(PROFILE, "Are you willing to relocate?", "select") == "No"
    assert _gpv(PROFILE, "Preferred language", "text") == "English"
    assert _gpv(PROFILE, "I agree to the terms of service", "checkbox") == "on"
    assert _gpv(PROFILE, "I agree to the privacy policy", "select") == "Yes"


# ── unknown / nonsense labels ─────────────────────────────────────────────────

def test_unknown_labels_return_none():
    assert _gpv(PROFILE, "Blorptastic frobnication quotient", "text") is None
    assert _gpv(PROFILE, "Xyzzy", "select") is None
    assert _gpv(PROFILE, "Have you been convicted of a felony?", "text") is None
    assert _gpv(PROFILE, "", "text") is None


# ── empty profile robustness ──────────────────────────────────────────────────

def test_empty_profile_does_not_raise():
    for label, kind in [
        ("First name", "text"),
        ("Email", "text"),
        ("Phone", "text"),
        ("Years of experience", "number"),
        ("Degree", "text"),
        ("LinkedIn URL", "text"),
        ("City", "text"),
        ("What is your gender?", "select"),
        ("Cover letter", "textarea"),
        ("Totally unknown field", "text"),
    ]:
        # Just assert it returns without raising; value may be None or "".
        _gpv({}, label, kind)
    assert _gpv({}, "First name", "text") is None
    assert _gpv({}, "Email", "text") is None
