"""Single source of truth for Phase 1 (discovery) search configuration.

Both the standalone ``search_retriever.py`` and the Dagster ``search_jobs_op``
(in ``scripts/dagster_retrievers.py``) import ``SEARCH_KEYWORDS`` from here, so
the query string is defined in exactly one place.

``SEARCH_KEYWORDS`` is passed verbatim as the LinkedIn Voyager ``keywords:``
field (see ``scripts/fetch.py:JobSearchRetriever``). That value is interpolated
into a comma-delimited, parenthesised query DSL, so the string MUST NOT contain
commas, parentheses or colons. Quoted phrases and the boolean ``OR`` operator
are supported by LinkedIn job search; ``requests`` percent-encodes the spaces
and double quotes automatically, so no manual URL-encoding is needed here.

If T13's ``config.py`` module lands, fold this constant into it (or have it
re-export ``SEARCH_KEYWORDS``) and update the two importers.
"""

# Narrowed 2026-08-28 (ticket T6): the previous "software engineer AI ML" was
# too broad and produced ~786 OffsiteApply skips vs 45 applied. Phrase-OR form
# targets the roles that actually match the applicant profile.
SEARCH_KEYWORDS = (
    '"software engineer" OR "machine learning engineer" '
    'OR "AI engineer" OR "data engineer"'
)

# Fail fast (at import, for both consumers) if the query would corrupt the
# Voyager query DSL. ValueError, not assert, so `python -O` can't strip it.
_FORBIDDEN = set(",():")
if _FORBIDDEN & set(SEARCH_KEYWORDS):
    raise ValueError(
        f"SEARCH_KEYWORDS may not contain any of ,():  — these are Voyager query DSL "
        f"delimiters. Offending: {sorted(_FORBIDDEN & set(SEARCH_KEYWORDS))}"
    )
