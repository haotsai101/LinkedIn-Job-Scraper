import time

import pandas as pd
import requests
import tenacity

from scripts import linkedin_auth
from scripts.helpers import get_value_by_path, strip_val


class VoyagerAuthError(Exception):
    """Raised on an HTTP 401 from the Voyager API — the session cookies are stale.

    Not retryable at the ``_voyager_get`` layer (tenacity can't re-login — it only
    has the request). The multi-account retrievers catch it once via
    ``_ReauthMixin._get``: refresh that account's ``storage_state`` with a real
    Playwright login, rebuild its ``requests.Session`` + headers, retry once. A
    second consecutive 401 propagates.
    """


class VoyagerRetryableError(Exception):
    """Raised on an HTTP 429 or 5xx from the Voyager API — a transient server-side
    condition. Retried with exponential backoff by ``_voyager_get``; after the
    final attempt it propagates so the caller can decide what to do."""


# Retry policy for the raw network call to LinkedIn's Voyager API:
#   - ConnectionError / Timeout  -> transient, retry
#   - HTTP 429 / 5xx             -> transient, retry (surfaced as VoyagerRetryableError)
#   - HTTP 401                   -> auth problem, do NOT retry (VoyagerAuthError)
#   - any other non-2xx          -> returned as-is, caller handles it
# ~2s base, doubling, capped at 60s; 4 attempts total; the final failure re-raises.
_voyager_retry = tenacity.retry(
    retry=tenacity.retry_if_exception_type(
        (
            requests.exceptions.ConnectionError,
            requests.exceptions.Timeout,
            VoyagerRetryableError,
        )
    ),
    wait=tenacity.wait_exponential(multiplier=2, max=60),
    stop=tenacity.stop_after_attempt(4),
    reraise=True,
)


@_voyager_retry
def _voyager_get(session, url, headers=None, timeout=30):
    """GET ``url`` on ``session`` with tenacity retry/backoff.

    Returns the ``requests.Response`` for a 2xx or any non-retryable non-2xx
    (e.g. 400/403/404) so the existing caller-side status checks still apply.
    Raises ``VoyagerAuthError`` on 401 (no retry) and ``VoyagerRetryableError``
    on 429/5xx (retried, then re-raised on the last attempt).
    """
    resp = session.get(url, headers=headers, timeout=timeout)
    if resp.status_code == 401:
        raise VoyagerAuthError(
            f'HTTP 401 from Voyager ({url}) — session cookies are stale, re-login required'
        )
    if resp.status_code == 429 or resp.status_code >= 500:
        raise VoyagerRetryableError(
            f'HTTP {resp.status_code} from Voyager ({url}) — transient, retrying'
        )
    return resp

def get_logins(method):
    logins = pd.read_csv('logins.csv')
    logins = logins[logins['method'] == method]
    emails = logins['emails'].tolist()
    passwords = logins['passwords'].tolist()
    return emails, passwords


class _ReauthMixin:
    """Shared multi-account session handling + one-shot 401 recovery.

    Both retrievers hold ``self.sessions`` (one authenticated ``requests.Session``
    per ``logins.csv`` account) and ``self.headers`` (the matching per-session
    Voyager header dict). ``_init_accounts`` builds them from ``storage_state``
    files — **no browser launch when a valid state file exists** (T17). ``_get``
    wraps a Voyager call so a single ``VoyagerAuthError`` triggers a real
    re-login for that one account and one retry; a second 401 propagates.

    Subclasses must implement ``_make_headers(idx)``.
    """

    def _init_accounts(self, method):
        self.emails, self.passwords = get_logins(method)
        self.state_paths = [linkedin_auth.state_path_for(e) for e in self.emails]
        self.sessions = [
            linkedin_auth.get_session(e, p, sp)
            for e, p, sp in zip(self.emails, self.passwords, self.state_paths, strict=True)
        ]

    def _make_headers(self, idx):  # pragma: no cover - overridden
        raise NotImplementedError

    def _reauth(self, idx):
        """Refresh account ``idx``'s session in place via a real Playwright login."""
        print(f'[fetch] 401 for {self.emails[idx]!r} — refreshing LinkedIn session')
        linkedin_auth.login_and_save_state(
            self.emails[idx], self.passwords[idx], self.state_paths[idx]
        )
        self.sessions[idx] = linkedin_auth.session_from_storage_state(self.state_paths[idx])
        self.headers[idx] = self._make_headers(idx)

    def _get(self, idx, url, headers=None):
        """``_voyager_get`` for account ``idx`` with one re-auth + retry on 401."""
        try:
            return _voyager_get(self.sessions[idx], url, headers=headers or self.headers[idx])
        except VoyagerAuthError:
            self._reauth(idx)
            return _voyager_get(self.sessions[idx], url, headers=self.headers[idx])

class JobSearchRetriever(_ReauthMixin):
    def __init__(self, keywords="data", count: int = 100, filters: str = "sortBy:List(DD)", geo_id: str = ""):
        """Create a search retriever.

        keywords: search keywords
        count: number of results per page (used to compute start offset)
        filters: LinkedIn selectedFilters string, e.g. "sortBy:List(DD),workplaceType:List(2)"
        geo_id: LinkedIn geoId for location (e.g. "102095887" for Utah); placed at top-level query
        """
        self.count = count
        geo_part = f",geoId:{geo_id}" if geo_id else ""
        query = f"keywords:{keywords}{geo_part},origin:JOB_SEARCH_PAGE_OTHER_ENTRY,selectedFilters:({filters}),spellCorrectionEnabled:true"
        # template with a {start} placeholder; get_jobs will replace start based on page
        self.job_search_link_template = f'https://www.linkedin.com/voyager/api/voyagerJobsDashJobCards?decorationId=com.linkedin.voyager.dash.deco.jobs.search.JobSearchCardsCollection-187&count={self.count}&q=jobSearch&query=({query})&start={{start}}'
        self._init_accounts('search')
        self.session_index = 0
        self.headers = [self._make_headers(i) for i in range(len(self.sessions))]

    def _make_headers(self, idx):
        session = self.sessions[idx]
        return {
            'Authority': 'www.linkedin.com',
            'Method': 'GET',
            # Note: 'Path' header is intentionally left out or will be overridden per-request to avoid stale start offsets
            'Scheme': 'https',
            'Accept': 'application/vnd.linkedin.normalized+json+2.1',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cookie': linkedin_auth.cookie_header(session),
            'Csrf-Token': linkedin_auth.csrf_token(session),
            # 'TE': 'Trailers',
            'User-Agent': linkedin_auth.USER_AGENT,
            # 'X-Li-Track': '{"clientVersion":"1.12.7990","mpVersion":"1.12.7990","osName":"web","timezoneOffset":-7,"timezone":"America/Los_Angeles","deviceFormFactor":"DESKTOP","mpName":"voyager-web","displayDensity":1,"displayWidth":1920,"displayHeight":1080}'
            'X-Li-Track': linkedin_auth.X_LI_TRACK,
        }

    def get_jobs(self, page: int = 0):
        """Fetch job cards for the given page (0-based). Page 0 == start=0.

        page: zero-based page index. start offset = page * count.
        """
        start = page * self.count
        link = self.job_search_link_template.format(start=start)

        # copy headers for this session and remove or override Path to avoid stale start params
        headers = dict(self.headers[self.session_index])
        if 'Path' in headers:
            headers.pop('Path')

        results = self._get(self.session_index, link, headers=headers)
        self.session_index = (self.session_index + 1) % len(self.sessions)

        if results.status_code != 200:
            raise Exception('Status code {} for search\nText: {}'.format(results.status_code, results.text))
        results = results.json()
        job_ids = {}

        for r in results['included']:
            if r['$type'] == 'com.linkedin.voyager.dash.jobs.JobPostingCard' and 'referenceId' in r:
                job_id = int(strip_val(r['jobPostingUrn'], 1))
                job_ids[job_id] = {'sponsored': False}
                job_ids[job_id]['title'] = r.get('jobPostingTitle')
                for x in r['footerItems']:
                    if x.get('type') == 'PROMOTED':
                        job_ids[job_id]['sponsored'] = True
                        break

        return job_ids

class JobDetailRetriever(_ReauthMixin):
    def __init__(self):
        self.error_count = 0
        self.job_details_link = "https://www.linkedin.com/voyager/api/jobs/jobPostings/{}?decorationId=com.linkedin.voyager.deco.jobs.web.shared.WebFullJobPosting-65"
        self._init_accounts('details')
        self.session_index = 0
        self.variable_paths = pd.read_csv('json_paths/data_variables.csv')
        self.headers = [self._make_headers(i) for i in range(len(self.sessions))]

        # self.proxies = [{'http': f'http://{proxy}', 'https': f'http://{proxy}'} for proxy in []]

    def _make_headers(self, idx):
        session = self.sessions[idx]
        return {
            'Authority': 'www.linkedin.com',
            'Method': 'GET',
            'Path': '/voyager/api/search/hits?decorationId=com.linkedin.voyager.deco.jserp.WebJobSearchHitWithSalary-25&count=25&filters=List(sortBy-%3EDD,resultType-%3EJOBS)&origin=JOB_SEARCH_PAGE_JOB_FILTER&q=jserpFilters&queryContext=List(primaryHitType-%3EJOBS,spellCorrectionEnabled-%3Etrue)&start=0&topNRequestedFlavors=List(HIDDEN_GEM,IN_NETWORK,SCHOOL_RECRUIT,COMPANY_RECRUIT,SALARY,JOB_SEEKER_QUALIFIED,PRE_SCREENING_QUESTIONS,SKILL_ASSESSMENTS,ACTIVELY_HIRING_COMPANY,TOP_APPLICANT)',
            'Scheme': 'https',
            'Accept': 'application/vnd.linkedin.normalized+json+2.1',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cookie': linkedin_auth.cookie_header(session),
            'Csrf-Token': linkedin_auth.csrf_token(session),
            # 'TE': 'Trailers',
            'User-Agent': linkedin_auth.USER_AGENT,
            # 'X-Li-Track': '{"clientVersion":"1.12.7990","mpVersion":"1.12.7990","osName":"web","timezoneOffset":-7,"timezone":"America/Los_Angeles","deviceFormFactor":"DESKTOP","mpName":"voyager-web","displayDensity":1,"displayWidth":1920,"displayHeight":1080}'
            'X-Li-Track': linkedin_auth.X_LI_TRACK,
        }


    def get_job_details(self, job_ids):
        job_details = {}
        for job_id in job_ids:
            error = False
            # A single 401 is recovered inside ``_get`` (re-login + one retry for
            # that account). A *second* consecutive 401 raises VoyagerAuthError,
            # which is intentionally NOT caught here — a session that won't
            # re-authenticate is not a per-job problem, so it propagates with a
            # clear message.
            try:
                details = self._get(
                    self.session_index,
                    self.job_details_link.format(job_id),
                )
            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout,
                    VoyagerRetryableError) as exc:
                # tenacity already retried 4x with backoff and it still failed.
                print(f'Network error for job {job_id} after retries: {exc}')
                job_details[job_id] = -1
                error = True
            else:
                if details.status_code != 200:
                    job_details[job_id] = -1
                    print('Status code {} for job {} with account {}\nText: {}'.format(
                        details.status_code, job_id, self.emails[self.session_index], details.text))
                    error = True
                else:
                    self.error_count = 0
                    job_details[job_id] = details.json()
                    print('Job {} done'.format(job_id))
            if error:
                self.error_count += 1
                if self.error_count > 10:
                    raise Exception('Too many errors')
            self.session_index = (self.session_index + 1) % len(self.sessions)
            time.sleep(.3)
        return job_details

# https://proxy2.webshare.io/register?

