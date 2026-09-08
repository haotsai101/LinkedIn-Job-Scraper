import random
from selenium import webdriver
from selenium.webdriver.common.by import By
import time
import requests
import pandas as pd
import tenacity

from scripts.helpers import strip_val, get_value_by_path


BROWSER = 'edge'


class VoyagerAuthError(Exception):
    """Raised on an HTTP 401 from the Voyager API — the session cookies are stale.

    Not retryable: refreshing the session is PR 2's job (Selenium -> Playwright
    cookie handling). For now this propagates with a clear message so the caller
    knows a re-login is required rather than a transient network blip.
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

def create_session(email, password):
    if BROWSER == 'chrome':
        driver = webdriver.Chrome()
    elif BROWSER == 'edge':
        driver = webdriver.Edge()

    driver.get('https://www.linkedin.com/checkpoint/rm/sign-in-another-account')
    time.sleep(1)
    driver.find_element(By.ID, 'username').send_keys(email)
    driver.find_element(By.ID, 'password').send_keys(password)
    driver.find_element(By.CSS_SELECTOR, 'button.btn__primary--large[type="submit"]').click()
    # Wait up to 30s for all auth redirect paths to clear
    _auth_paths = ('/checkpoint', '/login', '/challenge', '/security')
    print(f'[create_session] Waiting for login to complete for {email!r}...')
    deadline = time.time() + 30
    while time.time() < deadline:
        if not any(p in driver.current_url for p in _auth_paths):
            break
        time.sleep(1)
    if any(p in driver.current_url for p in _auth_paths):
        driver.quit()
        raise RuntimeError(f'Login failed or timed out for {email!r} — still on auth page: {driver.current_url}')
    driver.get('https://www.linkedin.com/jobs/search/?')
    time.sleep(1)
    cookies = driver.get_cookies()
    driver.quit()
    session = requests.Session()
    for cookie in cookies:
        session.cookies.set(cookie['name'], cookie['value'])
    return session

def get_logins(method):
    logins = pd.read_csv('logins.csv')
    logins = logins[logins['method'] == method]
    emails = logins['emails'].tolist()
    passwords = logins['passwords'].tolist()
    return emails, passwords

class JobSearchRetriever:
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
        emails, passwords = get_logins('search')
        self.sessions = [create_session(email, password) for email, password in zip(emails, passwords)]
        self.session_index = 0
        self.headers = [{
            'Authority': 'www.linkedin.com',
            'Method': 'GET',
            # Note: 'Path' header is intentionally left out or will be overridden per-request to avoid stale start offsets
            'Scheme': 'https',
            'Accept': 'application/vnd.linkedin.normalized+json+2.1',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cookie': "; ".join([f"{key}={value}" for key, value in session.cookies.items()]),
            'Csrf-Token': session.cookies.get('JSESSIONID').strip('"'),
            # 'TE': 'Trailers',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
            # 'X-Li-Track': '{"clientVersion":"1.12.7990","mpVersion":"1.12.7990","osName":"web","timezoneOffset":-7,"timezone":"America/Los_Angeles","deviceFormFactor":"DESKTOP","mpName":"voyager-web","displayDensity":1,"displayWidth":1920,"displayHeight":1080}'
            'X-Li-Track': '{"clientVersion":"1.13.5589","mpVersion":"1.13.5589","osName":"web","timezoneOffset":-7,"timezone":"America/Los_Angeles","deviceFormFactor":"DESKTOP","mpName":"voyager-web","displayDensity":1,"displayWidth":360,"displayHeight":800}'
        } for session in self.sessions]

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

        results = _voyager_get(self.sessions[self.session_index], link, headers=headers)
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

class JobDetailRetriever:
    def __init__(self):
        self.error_count = 0
        self.job_details_link = "https://www.linkedin.com/voyager/api/jobs/jobPostings/{}?decorationId=com.linkedin.voyager.deco.jobs.web.shared.WebFullJobPosting-65"
        emails, passwords = get_logins('details')
        self.emails = emails
        self.sessions = [create_session(email, password) for email, password in zip(emails, passwords)]
        self.session_index = 0
        self.variable_paths = pd.read_csv('json_paths/data_variables.csv')

        self.headers = [{
            'Authority': 'www.linkedin.com',
            'Method': 'GET',
            'Path': '/voyager/api/search/hits?decorationId=com.linkedin.voyager.deco.jserp.WebJobSearchHitWithSalary-25&count=25&filters=List(sortBy-%3EDD,resultType-%3EJOBS)&origin=JOB_SEARCH_PAGE_JOB_FILTER&q=jserpFilters&queryContext=List(primaryHitType-%3EJOBS,spellCorrectionEnabled-%3Etrue)&start=0&topNRequestedFlavors=List(HIDDEN_GEM,IN_NETWORK,SCHOOL_RECRUIT,COMPANY_RECRUIT,SALARY,JOB_SEEKER_QUALIFIED,PRE_SCREENING_QUESTIONS,SKILL_ASSESSMENTS,ACTIVELY_HIRING_COMPANY,TOP_APPLICANT)',
            'Scheme': 'https',
            'Accept': 'application/vnd.linkedin.normalized+json+2.1',
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept-Language': 'en-US,en;q=0.9',
            'Cookie': "; ".join([f"{key}={value}" for key, value in session.cookies.items()]),
            'Csrf-Token': session.cookies.get('JSESSIONID').strip('"'),
            # 'TE': 'Trailers',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/117.0.0.0 Safari/537.36',
            # 'X-Li-Track': '{"clientVersion":"1.12.7990","mpVersion":"1.12.7990","osName":"web","timezoneOffset":-7,"timezone":"America/Los_Angeles","deviceFormFactor":"DESKTOP","mpName":"voyager-web","displayDensity":1,"displayWidth":1920,"displayHeight":1080}'
            'X-Li-Track': '{"clientVersion":"1.13.5589","mpVersion":"1.13.5589","osName":"web","timezoneOffset":-7,"timezone":"America/Los_Angeles","deviceFormFactor":"DESKTOP","mpName":"voyager-web","displayDensity":1,"displayWidth":360,"displayHeight":800}'
        } for session in self.sessions]

        # self.proxies = [{'http': f'http://{proxy}', 'https': f'http://{proxy}'} for proxy in []]


    def get_job_details(self, job_ids):
        job_details = {}
        for job_id in job_ids:
            error = False
            # VoyagerAuthError (401) is intentionally NOT caught here — a stale
            # session is not a per-job problem, so it propagates with a clear
            # message (session refresh is PR 2's concern).
            try:
                details = _voyager_get(
                    self.sessions[self.session_index],
                    self.job_details_link.format(job_id),
                    headers=self.headers[self.session_index],
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

