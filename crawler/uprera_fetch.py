"""The plain-HTTP UP-RERA detail-page fetch chain — no browser needed.

Port of the chain in real_estate_backend's UpreraClient.java:

  1. GET the district list page. Keep its cookies and hidden ASP.NET fields.
  2. POST that row's "View Detail" postback -> 302 whose Location has binid=<...>.
  3. GET /Projectsummary?UI0aPA1ISD=<binid>&...   (fresh cookie jar)
  4. GET /viewprojects in that same cookie jar.

Shared by fetch_details.py (fetch-to-disk, for offline/debug use) and
process_details.py (the normal fetch+import-straight-into-Mongo pipeline).
"""
from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

BASE = "https://www.up-rera.in"
LIST_URL = BASE + "/frm_allprojectdistrictwise.aspx"
# Constant across projects in the Java client. If the summary page comes back
# blank, copy fresh values from a Projectsummary URL opened in a real browser.
SUMMARY_FLAGS = "&hfFlag=9emr4VdBw22M7BGjKtJWMPDI4s5cHQZP&IRSAHEB=D6PY3lyims8="
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
LIST_TTL_SECONDS = 6 * 3600   # how long a district list (and its form state) is reused
LIST_REFRESH_AFTER = 60       # refetch the list if a project is missing and the cache is older than this
TIMEOUT = (20, 150)           # connect, read. The site is slow (~45s per page).
RETRIES = 3

ROW_CTL_RE = re.compile(r"GridView1_(ctl\d+)_lblRegistrationNo")
BINID_RE = re.compile(r"binid=([^&]+)")


class FetchError(Exception):
    pass


@dataclass
class DistrictList:
    session: requests.Session  # keeps the cookies the postback must reuse
    url: str
    form: dict                 # hidden ASP.NET fields (__VIEWSTATE etc.)
    rows: dict                 # registration_no -> {"ctl": "ctl02", "serial": "1"}
    fetched_at: float


class UpreraFetcher:
    def __init__(self, delay: float):
        self.delay = delay
        self._lists: dict[str, DistrictList] = {}

    @staticmethod
    def _new_session() -> requests.Session:
        s = requests.Session()
        s.headers["User-Agent"] = USER_AGENT
        return s

    @staticmethod
    def _get(session: requests.Session, url: str, referer: str | None = None) -> str:
        headers = {"Referer": referer} if referer else {}
        last_exc: Exception | None = None
        for attempt in range(1, RETRIES + 1):
            try:
                # allow_redirects=False, as in the Java client: a redirect is treated as a failure.
                resp = session.get(url, headers=headers, timeout=TIMEOUT, allow_redirects=False)
            except requests.RequestException as exc:
                last_exc = exc
                time.sleep(3 * attempt)
                continue
            if resp.status_code != 200:
                raise FetchError(f"GET {url} returned HTTP {resp.status_code}")
            resp.encoding = "utf-8"
            return resp.text
        raise FetchError(f"GET {url} failed after {RETRIES} attempts: {last_exc}")

    def district_list(self, district: str, refresh: bool = False) -> DistrictList:
        cached = self._lists.get(district)
        if not refresh and cached and time.time() - cached.fetched_at < LIST_TTL_SECONDS:
            return cached

        url = f"{LIST_URL}?districtname={quote(district, safe='')}"
        session = self._new_session()
        soup = BeautifulSoup(self._get(session, url), "html.parser")

        form = {}
        for inp in soup.select("form input[type=hidden]"):
            if inp.get("name"):
                form[inp["name"]] = inp.get("value", "")

        rows = {}
        for span in soup.select("span[id$=_lblRegistrationNo]"):
            m = ROW_CTL_RE.search(span.get("id", ""))
            if not m:
                continue
            tr = span.find_parent("tr")
            first = tr.find("td") if tr else None
            rows[span.get_text(strip=True)] = {
                "ctl": m.group(1),
                "serial": first.get_text(strip=True) if first else None,
            }

        if not rows:
            raise FetchError(f'No project list for district "{district}" (check the spelling)')

        lst = DistrictList(session, url, form, rows, time.time())
        self._lists[district] = lst
        return lst

    @staticmethod
    def _postback(lst: DistrictList, ctl: str, reg: str) -> str:
        form = dict(lst.form)
        form["__EVENTTARGET"] = f"ctl00$ContentPlaceHolder1$GridView1${ctl}$LnkView"
        form["__EVENTARGUMENT"] = ""
        resp = lst.session.post(
            lst.url,
            data=form,
            headers={"Referer": lst.url},
            timeout=TIMEOUT,
            allow_redirects=False,
        )
        location = resp.headers.get("Location", "")
        m = BINID_RE.search(location)
        if resp.status_code != 302 or not m:
            raise FetchError(
                f'"View Detail" postback for {reg} returned HTTP {resp.status_code} '
                f"without a binid (Location: {location!r})"
            )
        return m.group(1)

    def resolve_binid(self, reg: str, district: str) -> str:
        lst = self.district_list(district)
        # A project registered after the list was cached: refetch once.
        if reg not in lst.rows and time.time() - lst.fetched_at > LIST_REFRESH_AFTER:
            lst = self.district_list(district, refresh=True)
        if reg not in lst.rows:
            raise FetchError(f'{reg} is not on the project list for "{district}"')
        try:
            return self._postback(lst, lst.rows[reg]["ctl"], reg)
        except FetchError as exc:
            # Cached form state went stale (the Java client does the same). Refetch and retry once.
            print(f"    postback failed ({exc}); refetching the district list", file=sys.stderr)
            lst = self.district_list(district, refresh=True)
            if reg not in lst.rows:
                raise FetchError(f'{reg} dropped off the project list for "{district}"')
            return self._postback(lst, lst.rows[reg]["ctl"], reg)

    def fetch_pages(self, binid: str) -> tuple[str, str]:
        # A fresh cookie jar per project: the summary request selects the project
        # in the session, and /viewprojects shows whichever project is selected.
        session = self._new_session()
        summary_url = f"{BASE}/Projectsummary?UI0aPA1ISD={quote(binid, safe='')}{SUMMARY_FLAGS}"
        summary_html = self._get(session, summary_url)
        viewprojects_html = self._get(session, f"{BASE}/viewprojects", referer=summary_url)
        return summary_html, viewprojects_html
