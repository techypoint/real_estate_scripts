#!/usr/bin/env python3
"""Fetch + import UP-RERA project details, one project at a time, straight into Mongo/R2.

Combines fetch_details.py and import_rera.py into a single streaming step: for
each project of the given district that's in Mongo but still missing full
detail (has_detail != true), this fetches its two detail pages, parses them,
uploads its documents to R2, and upserts the full record — before moving to
the next project. Nothing is written to disk by default, so there's nothing
to clean up afterwards; pass --keep-pages to save the raw HTML for debugging.

"Pending" is read straight from Mongo, so this works unattended across
districts: run `scrape_uprera.py --district X` (which upserts the bare list
into Mongo), then `process_details.py --district X` — no local JSON file has
to be carried between the two steps.

A project that fails is marked detail_fetch_status="failed" with the error
and is retried automatically on the next run — pass --skip-failed to leave
those alone instead.

Usage:
    python process_details.py --district "Gautam Buddha Nagar" --limit 5
    python process_details.py --district "Gautam Buddha Nagar" --dry-run
    nohup python process_details.py --district "Lucknow" > process.log 2>&1 &
"""
from __future__ import annotations

import argparse
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from parse_detail import is_shell as summary_is_shell, parse_detail
from parse_viewprojects import is_shell as viewprojects_is_shell, parse_viewproject
from uprera_fetch import FetchError, UpreraFetcher
from uprera_store import (
    COL_PROJECTS,
    ROOT,
    connect_mongo,
    ensure_indexes,
    load_env,
    map_detail_record,
    r2_client,
    required_env,
    resolve_document,
)


def pending_registrations(db, district: str, skip_failed: bool) -> list[str]:
    query: dict = {"district": district, "has_detail": {"$ne": True}}
    if skip_failed:
        query["detail_fetch_status"] = {"$ne": "failed"}
    cursor = db[COL_PROJECTS].find(query, {"registration_no": 1}).sort("sno", 1)
    return [doc["registration_no"] for doc in cursor if doc.get("registration_no")]


def process_one(
    db,
    s3,
    bucket: str | None,
    fetcher: UpreraFetcher,
    reg: str,
    district: str,
    pages_dir: Path | None,
    dry_run: bool,
) -> tuple[bool, str]:
    now = datetime.now(timezone.utc)
    try:
        binid = fetcher.resolve_binid(reg, district)
        summary_html, viewprojects_html = fetcher.fetch_pages(binid)

        summary = parse_detail(summary_html, source="summary.html")
        viewprojects = parse_viewproject(viewprojects_html, source="viewprojects.html")
        if summary_is_shell(summary) or viewprojects_is_shell(viewprojects):
            raise FetchError("got a blank page (the site may have changed or blocked this request)")

        if pages_dir is not None:
            page_dir = pages_dir / reg
            page_dir.mkdir(parents=True, exist_ok=True)
            (page_dir / "summary.html").write_text(summary_html, encoding="utf-8")
            (page_dir / "viewprojects.html").write_text(viewprojects_html, encoding="utf-8")

        raw_docs = viewprojects.get("documents") or []
        documents = [
            resolve_document(s3, bucket, reg, doc, pages_dir or ROOT / "data/pages", dry_run)
            for doc in raw_docs
        ]

        rec = {"registration_no": reg, "district": district, "summary": summary, "viewprojects": viewprojects}
        mapped = map_detail_record(rec)
        mapped["documents"] = documents
        mapped["detail_fetch_status"] = "done"
        mapped["detail_fetch_error"] = None
        mapped["detail_fetched_at"] = now
        mapped["updatedAt"] = now

        label = f"{mapped.get('project_name') or reg} ({len(documents)} documents)"
        if dry_run:
            return True, f"[dry-run] would save: {label}"

        db[COL_PROJECTS].update_one(
            {"registration_no": reg},
            {"$set": mapped, "$setOnInsert": {"createdAt": now}},
            upsert=True,
        )
        return True, f"saved: {label}"
    except Exception as exc:
        if not dry_run:
            db[COL_PROJECTS].update_one(
                {"registration_no": reg},
                {"$set": {"detail_fetch_status": "failed", "detail_fetch_error": str(exc), "updatedAt": now}},
            )
        return False, str(exc)


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Fetch + import UP-RERA project details one at a time, straight into Mongo/R2."
    )
    ap.add_argument("--district", default="Gautam Buddha Nagar", help="district name as used on up-rera.in")
    ap.add_argument("--limit", type=int, help="process at most this many projects in this run")
    ap.add_argument("--delay", type=float, default=2.0, help="seconds to wait between projects")
    ap.add_argument("--skip-failed", action="store_true", help="don't retry projects already marked failed")
    ap.add_argument("--keep-pages", action="store_true", help="save raw HTML under --pages-dir, for debugging")
    ap.add_argument("--pages-dir", default=str(ROOT / "data/pages"), help="only used with --keep-pages")
    ap.add_argument("--dry-run", action="store_true", help="do every step except the Mongo/R2 writes")
    args = ap.parse_args()

    env = load_env()
    client, db = connect_mongo(env)  # needed even in --dry-run, to read the pending list
    print(f"Connected to {db.name}")

    if args.dry_run:
        print("DRY RUN — no Mongo or R2 writes will happen.\n")
        s3 = bucket = None
    else:
        ensure_indexes(db)
        s3 = r2_client(env)
        bucket = required_env(env, "R2_BUCKET")

    todo = pending_registrations(db, args.district, args.skip_failed)
    print(f'{len(todo)} pending project(s) for "{args.district}".')
    if args.limit is not None:
        todo = todo[: args.limit]

    pages_dir = Path(args.pages_dir) if args.keep_pages else None
    fetcher = UpreraFetcher(args.delay)

    done = failed = 0
    for i, reg in enumerate(todo, 1):
        print(f"[{i}/{len(todo)}] {reg}")
        ok, msg = process_one(db, s3, bucket, fetcher, reg, args.district, pages_dir, args.dry_run)
        if ok:
            done += 1
            print(f"    {msg}")
        else:
            failed += 1
            print(f"    FAILED: {msg}", file=sys.stderr)
        if i < len(todo) and args.delay:
            time.sleep(args.delay)

    print(f"\nDone. {done} succeeded, {failed} failed, {len(todo)} attempted this run.")
    client.close()


if __name__ == "__main__":
    main()
