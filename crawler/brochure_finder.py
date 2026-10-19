#!/usr/bin/env python3
"""Find project brochures on builders' own websites — no search API needed.

UP-RERA has no brochure slot, but nearly every builder hosts its brochures on
its own site. Two steps:

  1. domains  — group RERA promoter names into brands (RERA lists ~800 promoter
                companies, mostly one SPV per project) and guess each brand's
                website, verified by the homepage naming one of its projects.
                Writes data/brand_domains.csv — a draft meant to be reviewed by
                hand: fix wrong domains, fill missing ones, set status=manual so
                later runs keep your edit.
  2. find     — per project: take its brand's website, find the project's own
                page there, and pull the brochure from that page:
                  a. WordPress sites: ask the site's media library / search API
                     for the project name — often returns the brochure directly
                  b. else the sitemap URL whose slug matches the project name
                  c. "Download Brochure" / .pdf links in that page's HTML
                  d. headless Chromium when the page is JS-rendered or the
                     brochure is behind a button: click it, catch the download.
                     A button that opens a lead form is recorded as "gated" —
                     we never submit fake enquiries.
                The best candidate is downloaded, checked (RERA number / project
                name in its text), uploaded to R2 under brochures/<reg>/ and
                saved as `brochure` on the Mongo record.

Usage:
    python brochure_finder.py domains --limit 100
    python brochure_finder.py find --reg UPRERAPRJ6695 -v
    python brochure_finder.py find --district "Gautam Buddha Nagar" --limit 20 --dry-run

Needs `pip install playwright pypdf` and `python -m playwright install chromium`.
"""
from __future__ import annotations

import argparse
import csv
import io
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests
from bs4 import BeautifulSoup

from uprera_store import COL_PROJECTS, connect_mongo, load_env, r2_client, r2_put_object, required_env

DATA = Path(__file__).resolve().parent / "data"
DOMAINS_CSV = DATA / "brand_domains.csv"
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/140.0 Safari/537.36"
)
TIMEOUT = (10, 40)
MAX_PDF_BYTES = 80 * 1024 * 1024

LEGAL_WORDS = {
    "m", "s", "ms", "pvt", "private", "ltd", "limited", "llp", "co", "company", "corp",
    "corporation", "the", "and", "of", "through", "thru", "consortium", "inc", "opc",
    "shree", "shri", "sri", "new",
}
# Words that say what a company does, not who it is — dropped from the brand key.
TRADE_WORDS = {
    "realtech", "realty", "realtors", "real", "estate", "estates", "infra", "infrastructure",
    "infratech", "infracon", "infraheights", "buildtech", "buildwell", "buildcon", "builders",
    "build", "developers", "developer", "development", "dev", "constructions", "construction",
    "housing", "homes", "properties", "property", "projects", "project", "promoters",
    "township", "townships", "india", "group", "associates", "enterprises", "ventures",
    "buildhome", "landcraft", "colonizers", "infraprojects", "realcon", "build-tech",
}
AUTHORITY_RE = re.compile(r"(?i)authority|parishad|nigam|nagar palika|awas vikas|\bgovt\b|government")
PARKED_RE = re.compile(r"(?i)domain (is )?for sale|buy this domain|parked free|sedoparking|hugedomains|domain has expired")
REALESTATE_RE = re.compile(r"(?i)apartments?\b|residential|real estate|flats\b|villas\b|\bbhk\b|township|possession")
# Namesakes (Logix fiber in Texas, Imperia pasta) pass a brand+"projects" check;
# an Indian builder's homepage nearly always names NCR/UP places or RERA.
LOCAL_RE = re.compile(r"(?i)noida|ghaziabad|delhi|\bncr\b|gurgaon|gurugram|uttar pradesh|lucknow|agra|kanpur|\brera\b|yamuna expressway")
DOMAIN_PATTERNS = ["{}.com", "{}india.com", "{}group.com", "{}.in", "{}.co.in", "{}infra.com",
                   "{}realty.com", "{}developers.com", "{}homes.com", "{}buildtech.com",
                   "{}group.in", "{}group.co.in", "{}infra.in", "{}developers.in", "{}homes.in"]

# Tokens too generic to identify a project on their own.
NAME_STOPWORDS = {
    "the", "of", "and", "at", "by", "in", "a", "phase", "tower", "towers", "block", "blocks",
    "sector", "plot", "no", "project", "residential", "residency", "commercial", "apartments",
    "apartment", "homes", "home", "group", "housing", "gh", "part", "extension", "ext",
    "noida", "greater", "west", "agra", "kanpur", "yamuna", "expressway", "i", "ii", "iii",
    "iv", "v", "vi", "pdf", "brochure", "ebrochure", "e", "final", "new", "compressed", "web",
    "low", "res", "lr", "hr", "copy", "revised", "wp", "content", "uploads", "pdfs", "assets",
    "www", "com", "in", "html", "php", "projects", "download", "file", "files",
}


def now() -> datetime:
    return datetime.now(timezone.utc)


def session() -> requests.Session:
    s = requests.Session()
    s.headers["User-Agent"] = USER_AGENT
    return s


def tokens(text: str) -> list[str]:
    text = unquote(text or "").lower()
    text = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", text)  # 2nd Parkview ~ 2-parkview
    return re.findall(r"[a-z]+|\d+", text)


# ---------------------------------------------------------------------------
# 1. promoter -> brand -> domain
# ---------------------------------------------------------------------------
def brand_key(promoter: str) -> str | None:
    """'Gaursons Realtech Private Limited' -> 'gaursons'. Two words when the first
    is just initials ('AR Landcraft' -> 'ar landcraft')."""
    if not promoter or AUTHORITY_RE.search(promoter):
        return None
    name = re.split(r"\(|\{|\[|;| through ", promoter, flags=re.I)[0]
    words = [w for w in tokens(name) if w not in LEGAL_WORDS and not w.isdigit()]
    core = [w for w in words if w not in TRADE_WORDS] or words
    if not core:
        return None
    if len(core[0]) <= 2 and len(words) > 1:
        return " ".join(words[:2])
    return core[0]


def check_homepage(sess: requests.Session, domain: str, brand: str, project_words: set[str]) -> tuple[int, str, str] | None:
    """Score how likely https://<domain>/ is this brand's real site. None if unreachable.
    The strongest signal is the homepage naming one of the brand's own RERA projects."""
    try:
        r = sess.get(f"https://{domain}/", timeout=(6, 20), allow_redirects=True)
    except requests.RequestException:
        return None
    if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
        return None
    html = r.text[:400_000]
    if PARKED_RE.search(html):
        return None
    soup = BeautifulSoup(html, "html.parser")
    title = " ".join((soup.title.get_text() if soup.title else "").split())
    first = brand.split()[0]
    score = 0
    if first in title.lower():
        score += 3
    if first in html.lower():
        score += 1
    if REALESTATE_RE.search(html):
        score += 1
    if LOCAL_RE.search(html):
        score += 2
    page_words = set(tokens(soup.get_text(" ")))
    if project_words & page_words:
        score += 3
    final = urlparse(r.url).netloc or domain
    return score, final, title


def guess_domain(item: tuple[str, list[str]]) -> tuple[str, str, int]:
    brand, project_names = item
    project_words = {t for n in project_names for t in tokens(n)
                     if len(t) > 3 and t not in NAME_STOPWORDS and t not in brand.split()}
    joined = brand.replace(" ", "")
    candidates = [p.format(joined) for p in DOMAIN_PATTERNS]
    sess = session()
    best = ("", "", 0)
    for cand in candidates:
        res = check_homepage(sess, cand, brand, project_words)
        if res and res[0] > best[2]:
            best = (res[1], res[2], res[0])
    # Brand in the title + real-estate + NCR/UP words = 7, +3 when it names one of
    # our projects. Below 6 it's usually a namesake or a JS shell page.
    return best if best[2] >= 6 else ("", best[1], best[2])


def read_domains() -> dict[str, dict]:
    if not DOMAINS_CSV.exists():
        return {}
    with DOMAINS_CSV.open(newline="", encoding="utf-8") as f:
        return {row["brand"]: row for row in csv.DictReader(f)}


def write_domains(rows: dict[str, dict]) -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    fields = ["brand", "domain", "status", "projects", "homepage_title", "score", "promoters"]
    ordered = sorted(rows.values(), key=lambda r: -int(r.get("projects") or 0))
    with DOMAINS_CSV.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(ordered)


def cmd_domains(db, args) -> None:
    query = {"district": args.district} if args.district else {}
    brands: dict[str, dict] = {}
    for doc in db[COL_PROJECTS].find(query, {"promoter_name": 1, "project_name": 1}):
        key = brand_key(doc.get("promoter_name") or "")
        if not key:
            continue
        b = brands.setdefault(key, {"brand": key, "projects": 0, "promoters": set(), "names": []})
        b["projects"] += 1
        b["promoters"].add(doc["promoter_name"].strip())
        b["names"].append(doc.get("project_name") or "")

    existing = read_domains()
    def needs_lookup(k: str) -> bool:
        status = (existing.get(k) or {}).get("status")
        return not status or (args.retry and status == "none")

    todo = [k for k in brands if needs_lookup(k)]
    todo.sort(key=lambda k: -brands[k]["projects"])
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(brands)} brands, {len(todo)} to look up ({len(existing)} already in {DOMAINS_CSV.name}).")

    # Drop rows for brand keys that no longer occur (renamed by a brand_key change)
    # unless someone filled them in by hand.
    rows = {k: v for k, v in existing.items() if k in brands or v.get("status") == "manual"}
    for key, b in brands.items():  # refresh counts/promoters for everything, keep domains
        row = rows.setdefault(key, {"brand": key, "domain": "", "status": "", "homepage_title": "", "score": ""})
        row["projects"] = b["projects"]
        row["promoters"] = " | ".join(sorted(b["promoters"]))

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        items = [(k, brands[k]["names"]) for k in todo]
        for key, (domain, title, score) in zip(todo, pool.map(guess_domain, items)):
            # "verified" = the homepage names one of this brand's projects; "auto"
            # rows are plausible but unconfirmed — check those by hand.
            status = "none" if not domain else "verified" if score >= 9 else "auto"
            rows[key].update(domain=domain, homepage_title=title[:120], score=score, status=status)
            print(f"  {key:22} {status:8} {domain or '-':32} {score:2}  {title[:55]}")

    write_domains(rows)
    found = sum(1 for r in rows.values() if r.get("domain"))
    print(f"\nWrote {DOMAINS_CSV} — {found}/{len(rows)} brands have a domain. "
          f"Review it; set status=manual on rows you fix by hand.")


# ---------------------------------------------------------------------------
# 2. one project -> its page on the builder's site -> its brochure
# ---------------------------------------------------------------------------
class Site:
    """One builder website. Its robots rules and sitemap page list (URLs only) are
    fetched once and reused for every project of that builder in this run."""

    def __init__(self, domain: str, delay: float):
        self.domain = domain
        self.base = f"https://{domain}"
        self.sess = session()
        self.delay = delay
        self._pages: list[str] | None = None
        self.robots = RobotFileParser()
        self.robot_sitemaps: list[str] = []
        try:
            r = self.sess.get(self.base + "/robots.txt", timeout=TIMEOUT)
            self.robots.parse(r.text.splitlines() if r.ok else [])
            if r.ok:
                self.robot_sitemaps = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", r.text)
        except requests.RequestException:
            self.robots.parse([])

    def allowed(self, url: str) -> bool:
        return self.robots.can_fetch(USER_AGENT, url)

    def get(self, url: str, **kw) -> requests.Response | None:
        if not self.allowed(url):
            return None
        try:
            r = self.sess.get(url, timeout=TIMEOUT, **kw)
            time.sleep(self.delay)
            return r if r.ok else None
        except requests.RequestException:
            return None

    def get_json(self, url: str, **kw):
        r = self.get(url, **kw)
        try:
            return r.json() if r is not None else None
        except ValueError:
            return None

    # WordPress sites (a large share of builder sites) answer these directly.
    def wp_pdfs(self, query: str) -> list[dict]:
        items = self.get_json(f"{self.base}/wp-json/wp/v2/media",
                              params={"search": query, "mime_type": "application/pdf", "per_page": 20})
        if not isinstance(items, list):
            return []
        return [{"url": it.get("source_url", ""),
                 "text": re.sub(r"<[^>]+>", "", (it.get("title") or {}).get("rendered", "")),
                 "page": it.get("link", ""), "via": "wp-media"} for it in items]

    def wp_pages(self, query: str) -> list[str]:
        items = self.get_json(f"{self.base}/wp-json/wp/v2/search", params={"search": query, "per_page": 10})
        if not isinstance(items, list):
            return []
        return [it["url"] for it in items if it.get("url")]

    @property
    def pages(self) -> list[str]:
        if self._pages is None:
            self._pages = self._sitemap_pages() or self._homepage_links()
        return self._pages

    def _sitemap_pages(self) -> list[str]:
        queue = list(dict.fromkeys(self.robot_sitemaps + [self.base + "/sitemap.xml", self.base + "/sitemap_index.xml"]))
        seen, pages = set(), []
        while queue and len(seen) < 40:
            sm = queue.pop(0)
            if sm in seen:
                continue
            seen.add(sm)
            r = self.get(sm)
            if r is None or "<loc>" not in r.text:
                continue
            for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text):
                loc = loc.replace("&amp;", "&")
                path = loc.lower().split("?")[0]
                if path.endswith(".xml"):
                    # blog/tag/author sitemaps never hold project pages
                    if not re.search(r"post-|tag|author|categor|news|blog|career", path):
                        queue.append(loc)
                elif not path.endswith(".pdf"):
                    pages.append(loc)
        return list(dict.fromkeys(pages))

    def _homepage_links(self) -> list[str]:
        r = self.get(self.base + "/")
        if r is None:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
        host = self.domain.removeprefix("www.")
        urls = []
        for a in soup.find_all("a", href=True):
            u = urljoin(r.url, a["href"]).split("#")[0]
            if urlparse(u).netloc.removeprefix("www.") == host:
                urls.append(u)
        return list(dict.fromkeys(urls))

    def scan_page(self, url: str) -> tuple[list[dict], bool]:
        """PDF links on one page, plus whether it needs a real browser to finish:
        a JS-rendered shell, or a "brochure" button with no plain PDF link."""
        r = self.get(url)
        if r is None or "html" not in r.headers.get("content-type", ""):
            return [], False
        soup = BeautifulSoup(r.text, "html.parser")
        pdfs, labelled = [], False
        for a in soup.find_all("a", href=True):
            href = urljoin(r.url, a["href"])
            text = a.get_text(" ", strip=True) + " " + (a.get("title") or "")
            if is_pdf(href):
                pdfs.append({"url": href, "text": text, "page": url, "via": "page"})
                labelled |= "brochure" in (text + href).lower()
        for m in re.finditer(r"""["']([^"'\s<>]+?\.pdf)(?:\?[^"']*)?["']""", r.text, re.I):
            pdfs.append({"url": urljoin(r.url, m.group(1)), "text": "", "page": url, "via": "page-js"})
        visible = soup.get_text(" ", strip=True)
        needs_browser = len(visible) < 1500 or ("brochure" in visible.lower() and not labelled)
        return pdfs, needs_browser


class Browser:
    """Headless Chromium, started on first use and shared across projects."""

    def __init__(self):
        self._pw = self._browser = None

    def scan_page(self, url: str) -> tuple[list[dict], bool]:
        """Render the page, collect PDF links, click "brochure" buttons and catch
        what they open. Returns (pdfs, gated) — gated when a click only brought
        up a lead form. We never fill those in."""
        if self._browser is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch()
        ctx = self._browser.new_context(accept_downloads=True, user_agent=USER_AGENT)
        pdfs: list[dict] = []
        gated = False

        def found(u: str, text: str, via: str) -> None:
            pdfs.append({"url": u, "text": text, "page": url, "via": via})

        ctx.on("response", lambda resp: found(resp.url, "brochure", "browser-net")
               if "pdf" in (resp.headers.get("content-type") or "") else None)
        page = ctx.new_page()
        try:
            try:
                page.goto(url, wait_until="networkidle", timeout=45_000)
            except Exception:
                page.goto(url, wait_until="load", timeout=45_000)
            for text, href in page.eval_on_selector_all(
                    "a[href]", "els => els.map(e => [e.innerText || e.title || '', e.href])"):
                if is_pdf(href):
                    found(href, text, "browser")

            buttons = page.locator("a, button, [role=button], span, div").filter(has_text=re.compile(r"brochure", re.I))
            tried = 0
            for i in range(min(buttons.count(), 8)):
                el = buttons.nth(i)
                try:
                    # skip hidden ones and big containers that merely contain the word
                    if not el.is_visible() or el.evaluate("e => e.children.length") > 3:
                        continue
                except Exception:
                    continue
                tried += 1
                if tried > 3:
                    break
                forms_before = page.locator("form input:visible").count()
                try:
                    with page.expect_download(timeout=5_000) as dl:
                        el.click(timeout=4_000)
                    found(dl.value.url, "brochure", "browser-download")
                    continue
                except Exception:
                    pass
                page.wait_for_timeout(1500)
                for other in ctx.pages:  # opened in a new tab
                    if other is not page:
                        found(other.url, "brochure", "browser-popup")
                        other.close()
                if page.url != url:
                    found(page.url, "brochure", "browser-nav")
                    page.goto(url, wait_until="load", timeout=45_000)
                elif page.locator("form input:visible").count() > forms_before:
                    gated = True
                    page.keyboard.press("Escape")
        except Exception as exc:
            print(f"    browser: {url}: {type(exc).__name__}", file=sys.stderr)
        finally:
            ctx.close()
        return [p for p in pdfs if is_pdf(p["url"]) or p["via"] != "browser"], gated

    def close(self) -> None:
        if self._browser is not None:
            self._browser.close()
            self._pw.stop()


def is_pdf(url: str) -> bool:
    return urlparse(url).path.lower().endswith(".pdf")


def project_words(name: str, brand: str) -> tuple[list[str], set[str], set[str]]:
    """'7th Avenue - Gaur City' (brand gaursons) -> query words ['7th', 'Avenue', 'City'],
    letter tokens {'avenue', 'city'}, digit tokens {'7'}. Brand-ish words ('gaur'
    for gaursons) are dropped: every page and PDF on the site carries them."""
    brand_flat = brand.replace(" ", "")

    def brandish(t: str) -> bool:
        return len(t) > 2 and (brand_flat.startswith(t) or t.startswith(brand_flat))

    raw = re.sub(r"\(.*?\)", " ", name or "")
    words = []
    for w in re.findall(r"[A-Za-z0-9]+", raw):
        toks = tokens(w)
        if toks and all(t not in NAME_STOPWORDS and not brandish(t) for t in toks):
            words.append(w)
    toks = {t for w in words for t in tokens(w)}
    return words[:4], {t for t in toks if not t.isdigit()}, {t for t in toks if t.isdigit()}


def name_score(text: str, letters: set[str], digits: set[str]) -> float:
    """Share of the project's name words found in text. 0 when the text carries a
    different number ('14th Avenue' is not '7th Avenue')."""
    toks = set(tokens(text))
    if not letters:
        return 0.0
    hit = letters & toks
    if not hit:
        return 0.0
    text_digits = {t for t in toks if t.isdigit() and len(t) <= 3}
    if digits and text_digits and not (digits & text_digits):
        return 0.0
    score = len(hit) / len(letters)
    if digits and digits & text_digits:
        score += 0.2
    return score


JUNK_PDF_RE = re.compile(r"(?i)price|payment|application|form|disclaimer|newsletter|annual|report|"
                         r"policy|result|notice|possession|news|press|csr|tender|invoice")


def rank_pdfs(pdfs: list[dict], letters: set[str], digits: set[str], page_scores: dict[str, float]) -> list[tuple[float, dict]]:
    seen, ranked = set(), []
    for p in pdfs:
        url = p["url"].split("#")[0]
        if url in seen:
            continue
        seen.add(url)
        file_part = unquote(urlparse(url).path.rsplit("/", 1)[-1])
        label = f"{file_part} {p.get('text', '')}"
        is_brochure = "brochure" in label.lower()
        score = name_score(label, letters, digits)
        # A brochure link on the project's own page counts even when the file
        # is named oddly (aero-heights page -> Airocity.pdf).
        if is_brochure and p.get("page") in page_scores:
            score = max(score, page_scores[p["page"]])
        if score == 0:
            continue
        if is_brochure:
            score += 0.3
        elif JUNK_PDF_RE.search(label):
            score -= 0.5
        ranked.append((round(score, 2), {**p, "url": url}))
    ranked.sort(key=lambda x: -x[0])
    return ranked


def find_brochure(site: Site, browser: Browser | None, name: str, brand: str, verbose: bool) -> tuple[list[tuple[float, dict]], bool]:
    """Ranked brochure candidates for one project, plus whether its page had a
    form-gated brochure button."""
    words, letters, digits = project_words(name, brand)
    if not letters:
        return [], False
    queries = list(dict.fromkeys([" ".join(words), " ".join(words[:2])]))
    pdfs: list[dict] = []

    # 1. WordPress: ask the site's own media library / search for this project
    for q in queries:
        pdfs += site.wp_pdfs(q)
    wp_pages = [u for q in queries for u in site.wp_pages(q)]

    # 2. the project's page: from WP search, else the sitemap URL whose slug matches
    page_scores: dict[str, float] = {}
    for u in wp_pages + site.pages:
        path = urlparse(u).path
        if re.search(r"(?i)blog|news|career|event|award|csr|investor|media|tag/|author/", path):
            continue
        s = name_score(path, letters, digits)
        if s >= 0.5:
            page_scores[u] = max(page_scores.get(u, 0), s)
    project_pages = sorted(page_scores, key=page_scores.get, reverse=True)[:3]
    if verbose:
        print(f"    query={queries} pages={project_pages} wp_pdfs={len(pdfs)}")

    # 3. brochure links on those pages; headless browser only when the HTML isn't enough
    gated = False
    for u in project_pages:
        found, needs_browser = site.scan_page(u)
        pdfs += found
        if needs_browser and browser is not None and site.allowed(u):
            found, page_gated = browser.scan_page(u)
            pdfs += found
            gated |= page_gated
    return rank_pdfs(pdfs, letters, digits, page_scores), gated


def fetch_pdf(url: str) -> bytes | None:
    try:
        with session().get(url, timeout=(10, 120), stream=True) as r:
            if not r.ok:
                return None
            buf = io.BytesIO()
            for chunk in r.iter_content(1 << 16):
                buf.write(chunk)
                if buf.tell() > MAX_PDF_BYTES:
                    return None
            data = buf.getvalue()
            return data if data[:5] == b"%PDF-" else None
    except requests.RequestException:
        return None


def pdf_text(data: bytes, max_pages: int = 8) -> tuple[str, int]:
    import logging

    from pypdf import PdfReader

    logging.getLogger("pypdf").setLevel(logging.ERROR)  # font warnings on every brochure
    try:
        reader = PdfReader(io.BytesIO(data))
        text = " ".join((reader.pages[i].extract_text() or "") for i in range(min(max_pages, len(reader.pages))))
        return text, len(reader.pages)
    except Exception:
        return "", 0


def confidence(reg: str, letters: set[str], url: str, text: str) -> str:
    if reg.upper() in re.sub(r"\s+", "", text).upper():
        return "high"
    if letters and letters <= set(tokens(text)):
        return "medium"
    # Brochures are often image-only PDFs with no text to check; trust a file
    # the builder itself named "<project>...brochure".
    file_part = unquote(urlparse(url).path.rsplit("/", 1)[-1]).lower()
    if "brochure" in file_part and letters and letters <= set(tokens(file_part)):
        return "medium"
    return "low"


def pending_projects(db, args) -> list[dict]:
    """Projects whose RERA detail is fetched (detail_fetch_status=done) and that
    haven't had a brochure lookup yet — in the same order process_details.py
    works through them. --force re-checks ones already looked up."""
    query: dict = {"detail_fetch_status": "done"}
    if args.reg:
        query = {"registration_no": args.reg}
    elif not args.force:
        query["brochure_status"] = {"$exists": False}
    if args.district:
        query["district"] = args.district
    fields = {"registration_no": 1, "project_name": 1, "promoter_name": 1}
    cursor = db[COL_PROJECTS].find(query, fields).sort([("district", 1), ("sno", 1)])
    if args.limit:
        cursor = cursor.limit(args.limit)
    return list(cursor)


def cmd_find(db, args) -> None:
    domains = read_domains()
    if not domains:
        raise SystemExit(f"{DOMAINS_CSV} missing — run `brochure_finder.py domains` first.")

    todo = pending_projects(db, args)
    print(f"{len(todo)} project(s) to look up.")

    s3 = bucket = None
    if not args.dry_run:
        env = load_env()
        s3 = r2_client(env)
        bucket = required_env(env, "R2_BUCKET")

    sites: dict[str, Site] = {}
    browser = None if args.no_browser else Browser()
    stats = {"found": 0, "not_found": 0, "gated": 0, "no_domain": 0}
    try:
        for i, proj in enumerate(todo, 1):
            reg, name = proj["registration_no"], proj.get("project_name") or ""
            brand = brand_key(proj.get("promoter_name") or "") or ""
            print(f"[{i}/{len(todo)}] {reg} {name} ({brand or proj.get('promoter_name')})")
            domain = (domains.get(brand) or {}).get("domain") if brand else None
            if not domain:
                stats["no_domain"] += 1
                print("    no website known for this brand")
                save_status(db, reg, "no_domain", args.dry_run)
                continue
            site = sites.setdefault(domain, Site(domain, args.delay))
            ranked, gated = find_brochure(site, browser, name, brand, args.verbose)
            _, letters, _ = project_words(name, brand)

            chosen = None
            for score, cand in ranked[:3]:
                if score < 0.6:
                    break
                data = fetch_pdf(cand["url"])
                if not data:
                    continue
                text, pages = pdf_text(data)
                conf = confidence(reg, letters, cand["url"], text)
                if conf == "low" and not args.accept_low:
                    print(f"    skip (low confidence, score {score}): {cand['url']}")
                    continue
                chosen = (cand, data, pages, score, conf)
                break

            if not chosen:
                status = "gated" if gated else "not_found"
                stats[status] += 1
                print(f"    {status.replace('_', ' ')}")
                save_status(db, reg, status, args.dry_run)
                continue

            cand, data, pages, score, conf = chosen
            stats["found"] += 1
            print(f"    FOUND ({conf}, score {score}, via {cand['via']}): {cand['url']} — {len(data) // 1024} KB, {pages} pages")
            if not args.dry_run:
                store_brochure(db, s3, bucket, reg, cand, data, pages, score, conf)
    finally:
        if browser is not None:
            browser.close()

    print(f"\nDone. {stats['found']} found, {stats['not_found']} not found, "
          f"{stats['gated']} behind a lead form, {stats['no_domain']} without a known website.")


def save_status(db, reg: str, status: str, dry_run: bool) -> None:
    if not dry_run:
        db[COL_PROJECTS].update_one({"registration_no": reg},
                                    {"$set": {"brochure_status": status, "brochure_checked_at": now()}})


def store_brochure(db, s3, bucket: str, reg: str, cand: dict, data: bytes, pages: int, score: float, conf: str) -> None:
    file_name = re.sub(r"[^\w.\-]+", "_", unquote(urlparse(cand["url"]).path.rsplit("/", 1)[-1])) or "brochure.pdf"
    key = f"brochures/{reg}/{file_name}"
    r2_put_object(s3, bucket, key, data, "application/pdf", file_name)
    ts = now()
    db[COL_PROJECTS].update_one({"registration_no": reg}, {"$set": {
        "brochure": {
            "url": key,
            "file_name": file_name,
            "source_url": cand["url"],
            "page_url": cand.get("page"),
            "found_via": cand.get("via"),
            "size": len(data),
            "pages": pages,
            "match_score": score,
            "confidence": conf,
            "fetched_at": ts,
        },
        "brochure_status": "found",
        "brochure_checked_at": ts,
    }})


def main() -> None:
    ap = argparse.ArgumentParser(description="Find project brochures on builders' own websites.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("domains", help="map promoter brands to websites -> data/brand_domains.csv")
    d.add_argument("--district", help="only brands with projects in this district")
    d.add_argument("--limit", type=int, help="look up at most this many new brands (biggest first)")
    d.add_argument("--workers", type=int, default=8)
    d.add_argument("--retry", action="store_true", help="re-guess brands whose last lookup found nothing")

    f = sub.add_parser("find", help="find each project's brochure and store it in R2/Mongo")
    f.add_argument("--reg", help="just this registration number")
    f.add_argument("--district")
    f.add_argument("--limit", type=int)
    f.add_argument("--delay", type=float, default=1.0, help="seconds between requests to a site")
    f.add_argument("--no-browser", action="store_true", help="skip the headless-Chromium step")
    f.add_argument("--force", action="store_true", help="also re-check projects already looked up")
    f.add_argument("--accept-low", action="store_true", help="also store low-confidence matches")
    f.add_argument("--dry-run", action="store_true", help="no R2/Mongo writes")
    f.add_argument("-v", "--verbose", action="store_true", help="print the pages each lookup tried")

    args = ap.parse_args()
    client, db = connect_mongo(load_env())
    try:
        {"domains": cmd_domains, "find": cmd_find}[args.cmd](db, args)
    finally:
        client.close()


if __name__ == "__main__":
    main()
