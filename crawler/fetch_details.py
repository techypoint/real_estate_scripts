#!/usr/bin/env python3
"""Fetch full UP-RERA project details (summary + "View Project In Detail") to local files.

This is the offline/debug path: it writes raw HTML and parsed JSON to disk
under --out-dir, with nothing going to Mongo or R2. For the normal day-to-day
workflow, prefer process_details.py, which fetches and imports straight into
Mongo/R2 one project at a time and never touches disk at all.

Output, under --out-dir (default ../data):
  pages/<reg>/summary.html, pages/<reg>/viewprojects.html   raw HTML, so the parsers can be re-run
  project_details/<reg>.json                                one file per project: {summary, viewprojects}
  project_details.json                                      all finished projects combined

Projects that already have a file in project_details/ are skipped, so an
interrupted run resumes where it stopped. Pages that come back as the blank
shell are not saved as done, so the next run retries them.

See uprera_fetch.py for the actual HTTP chain (shared with process_details.py).

Examples:
    python fetch_details.py --district "Gautam Buddha Nagar" --limit 5
    python fetch_details.py --district "Lucknow" --regs UPRERAPRJ125561,UPRERAPRJ123456
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

from parse_detail import is_shell as summary_is_shell, parse_detail
from parse_viewprojects import is_shell as viewprojects_is_shell, parse_viewproject
from uprera_fetch import FetchError, UpreraFetcher


def done_path(project_dir: Path, reg: str) -> Path:
    return project_dir / f"{reg}.json"


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch UP-RERA project details over plain HTTP.")
    ap.add_argument("--district", default="Gautam Buddha Nagar", help="district name as used on up-rera.in")
    ap.add_argument("--regs", help="comma-separated registration numbers (default: every project in the district)")
    ap.add_argument("--limit", type=int, help="fetch at most this many projects in this run")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds to wait between projects")
    ap.add_argument("--out-dir", default="../data", help="output root (pages/, project_details/)")
    args = ap.parse_args()

    out = Path(args.out_dir)
    project_dir = out / "project_details"
    project_dir.mkdir(parents=True, exist_ok=True)

    fetcher = UpreraFetcher(args.delay)
    print(f'Loading project list for "{args.district}" ...')
    lst = fetcher.district_list(args.district)
    regs = list(lst.rows)
    if args.regs:
        wanted = [r.strip() for r in args.regs.split(",") if r.strip()]
        missing = [r for r in wanted if r not in lst.rows]
        if missing:
            print(f"  not on the list, skipping: {', '.join(missing)}", file=sys.stderr)
        regs = [r for r in wanted if r in lst.rows]

    todo = [r for r in regs if not done_path(project_dir, r).exists()]
    print(f"{len(regs)} project(s) selected, {len(regs) - len(todo)} already done, {len(todo)} to fetch.")
    if args.limit is not None:
        todo = todo[: args.limit]

    failures = []
    for i, reg in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {reg}")
        try:
            binid = fetcher.resolve_binid(reg, args.district)
            summary_html, viewprojects_html = fetcher.fetch_pages(binid)

            page_dir = out / "pages" / reg
            page_dir.mkdir(parents=True, exist_ok=True)
            (page_dir / "summary.html").write_text(summary_html, encoding="utf-8")
            (page_dir / "viewprojects.html").write_text(viewprojects_html, encoding="utf-8")

            summary = parse_detail(summary_html, source="summary.html")
            viewprojects = parse_viewproject(viewprojects_html, source="viewprojects.html")
            if summary_is_shell(summary) or viewprojects_is_shell(viewprojects):
                raise FetchError("got a blank page (the site may have changed or blocked this request)")

            record = {"registration_no": reg, "district": args.district, "summary": summary, "viewprojects": viewprojects}
            done_path(project_dir, reg).write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"    saved: {summary.get('project_name') or reg}")
        except (FetchError, requests.RequestException) as exc:
            failures.append(reg)
            print(f"    FAILED: {exc}", file=sys.stderr)
        if i < len(todo) and args.delay:
            time.sleep(args.delay)

    combined = []
    for p in sorted(project_dir.glob("*.json")):
        combined.append(json.loads(p.read_text(encoding="utf-8")))
    combined_path = out / "project_details.json"
    combined_path.write_text(json.dumps(combined, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nDone. {len(todo) - len(failures)} fetched, {len(failures)} failed.")
    if failures:
        print("  failed: " + ", ".join(failures))
    print(f"  {combined_path} ({len(combined)} projects total)")


if __name__ == "__main__":
    main()
