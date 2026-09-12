#!/usr/bin/env python3
"""Scrape the UP-RERA registered projects list for a given district.

Data source: https://www.up-rera.in/frm_allprojectdistrictwise.aspx?districtname=<district>
This is a plain server-rendered ASP.NET page (a GridView control) that lists every
registered project in the given district. It needs no login/session and is fetched
with a single GET request.

Note: the per-project "View Detail" pages (promoter mobile/email, registration
dates, land info, etc.) are intentionally out of scope -- those pages render blank
for every automated client tested (plain requests and a scripted headless browser
following the exact same click/redirect a human would), while a normal interactive
browser session renders them fine. That pattern indicates the site is blocking
automated access to the pages that expose promoter contact details, so this script
does not attempt to work around it.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

LIST_URL = "https://www.up-rera.in/frm_allprojectdistrictwise.aspx"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "_", text)
    return text.strip("_")


def fetch_district_html(district: str, session: requests.Session, retries: int = 3) -> str:
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(
                LIST_URL,
                params={"districtname": district},
                headers={"User-Agent": USER_AGENT},
                timeout=60,
            )
            resp.raise_for_status()
            return resp.text
        except requests.RequestException as exc:
            last_exc = exc
            print(f"  fetch attempt {attempt}/{retries} failed: {exc}", file=sys.stderr)
            time.sleep(2 * attempt)
    assert last_exc is not None
    raise last_exc


def parse_projects(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", id="ctl00_ContentPlaceHolder1_GridView1")
    if table is None:
        return []

    projects = []
    for row in table.find_all("tr"):
        if row.find("th"):
            continue  # header row

        cells = row.find_all("td")
        if len(cells) < 6:
            continue

        sno = cells[0].get_text(strip=True)

        reg_span = cells[1].find("span")
        registration_no = reg_span.get_text(strip=True) if reg_span else cells[1].get_text(strip=True)

        name_span = cells[2].find("span")
        project_name = name_span.get_text(strip=True) if name_span else cells[2].get_text(strip=True)

        promoter_span = cells[3].find("span")
        if promoter_span:
            promoters = [li.get_text(strip=True) for li in promoter_span.find_all("li")]
            promoter_name = "; ".join(p for p in promoters if p)
        else:
            promoter_name = cells[3].get_text(strip=True)

        district_span = cells[4].find("span")
        district = district_span.get_text(strip=True) if district_span else cells[4].get_text(strip=True)

        type_span = cells[5].find("span")
        project_type = type_span.get_text(strip=True) if type_span else cells[5].get_text(strip=True)

        projects.append(
            {
                "sno": sno,
                "registration_no": registration_no,
                "project_name": project_name,
                "promoter_name": promoter_name,
                "district": district,
                "project_type": project_type,
            }
        )

    return projects


def save_csv(projects: list[dict], path: Path) -> None:
    fieldnames = ["sno", "registration_no", "project_name", "promoter_name", "district", "project_type"]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(projects)


def save_json(projects: list[dict], path: Path) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(projects, f, ensure_ascii=False, indent=2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape UP-RERA registered projects for a district.")
    parser.add_argument("--district", default="Gautam Buddha Nagar", help="District name as used on up-rera.in")
    parser.add_argument("--out-dir", default="data", help="Output directory for CSV/JSON files")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    session = requests.Session()
    print(f"Fetching project list for district: {args.district!r} ...")
    html = fetch_district_html(args.district, session)

    projects = parse_projects(html)
    if not projects:
        print("No projects found -- the page structure may have changed, or the district name didn't match.", file=sys.stderr)
        sys.exit(1)

    slug = slugify(args.district)
    csv_path = out_dir / f"{slug}_projects.csv"
    json_path = out_dir / f"{slug}_projects.json"
    save_csv(projects, csv_path)
    save_json(projects, json_path)

    print(f"Saved {len(projects)} projects to:")
    print(f"  {csv_path}")
    print(f"  {json_path}")


if __name__ == "__main__":
    main()
