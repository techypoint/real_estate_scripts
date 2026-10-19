#!/usr/bin/env python3
"""Batch-import UP-RERA data from local files into MongoDB + R2.

Python port of import_rera.mjs. This is the offline/file-based path: it
reads data/project_details.json (fetch_details.py's output) and a district's
list JSON, in one batch, after the fact.

For the normal day-to-day workflow, prefer process_details.py, which fetches
and imports straight into Mongo/R2 one project at a time and needs neither of
those local files. Use this one to backfill from files you already have, or
when you'd rather review everything before any Mongo/R2 write happens.

1. Upserts the bare district project list (name/promoter/type only).
2. Merges full-detail records (data/project_details.json), enriching the
   matching project by registration_no.
3. Uploads each referenced document PDF to R2 under documents/<reg>/<file> —
   preferring an already-downloaded local file (data/pages/<reg>/..., if you
   saved the PDFs there), falling back to the public blob_url. Idempotent:
   re-running skips keys that already exist in R2.

This collection is machine-owned and disposable — see real_estate_scripts'
CLAUDE.md, "The two data layers". Curated content in `projectcontents` is
never touched here.

Usage:
    python import_rera.py --dry-run                 # no Mongo/R2 writes, just report
    python import_rera.py
    python import_rera.py --details ../data/project_details.json --files ../data/pages
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

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
    upsert_list_rows,
)


# ---------------------------------------------------------------------------
# Pass 1: the bare district list
# ---------------------------------------------------------------------------
def import_list(db, list_path: Path, dry_run: bool) -> None:
    if not list_path.exists():
        print(f"(skip) list file not found: {list_path}")
        return

    rows = json.loads(list_path.read_text(encoding="utf-8"))
    print(f"List: {len(rows)} projects from {list_path.name}")

    if dry_run:
        print(f"  [dry-run] would upsert {len([r for r in rows if r.get('registration_no')])} list rows")
        return

    n = upsert_list_rows(db, rows)
    print(f"List import done ({n} upserts).")


# ---------------------------------------------------------------------------
# Pass 2: full details
# ---------------------------------------------------------------------------
def import_details(db, s3, bucket: str, details_path: Path, files_dir: Path, dry_run: bool) -> None:
    if not details_path.exists():
        print(f"(skip) details file not found: {details_path}")
        return

    records = json.loads(details_path.read_text(encoding="utf-8"))
    print(f"Details: {len(records)} record(s) from {details_path.name}")

    for rec in records:
        reg_no = rec.get("registration_no")
        if not reg_no:
            continue

        raw_docs = (rec.get("viewprojects") or {}).get("documents") or []
        documents = [resolve_document(s3, bucket, reg_no, doc, files_dir, dry_run) for doc in raw_docs]

        mapped = map_detail_record(rec)
        mapped["documents"] = documents
        mapped["updatedAt"] = datetime.now(timezone.utc)

        if dry_run:
            print(f"  [dry-run] would merge detail: {reg_no} — {mapped.get('project_name')} ({len(documents)} documents)")
            continue

        db[COL_PROJECTS].update_one(
            {"registration_no": reg_no},
            {"$set": mapped, "$setOnInsert": {"createdAt": datetime.now(timezone.utc)}},
            upsert=True,
        )
        print(f"  merged detail: {reg_no} — {mapped.get('project_name')} ({len(documents)} documents)")


def main() -> None:
    ap = argparse.ArgumentParser(description="Import UP-RERA data into MongoDB + R2 (Python, no converter step).")
    ap.add_argument("--list", default=str(ROOT / "data/gautam_buddha_nagar_projects.json"))
    ap.add_argument("--details", default=str(ROOT / "data/project_details.json"))
    ap.add_argument("--files", default=str(ROOT / "data/pages"))
    ap.add_argument("--dry-run", action="store_true", help="do every step except the Mongo/R2 writes")
    args = ap.parse_args()

    env = load_env()
    list_path, details_path, files_dir = Path(args.list), Path(args.details), Path(args.files)

    if args.dry_run:
        print("DRY RUN — no Mongo or R2 writes will happen.\n")
        db = None
        s3 = bucket = None
    else:
        client, db = connect_mongo(env)
        print(f"Connected to {db.name}")
        ensure_indexes(db)
        s3 = r2_client(env)
        bucket = required_env(env, "R2_BUCKET")

    import_list(db, list_path, args.dry_run)
    import_details(db, s3, bucket, details_path, files_dir, args.dry_run)

    if not args.dry_run:
        col = db[COL_PROJECTS]
        total = col.count_documents({})
        with_detail = col.count_documents({"has_detail": True})
        listed = col.count_documents({"listed": True})
        print(f"\nDone. projects: {total} total, {with_detail} with full detail, {listed} listed.")
        client.close()
    else:
        print("\nDry run done — nothing was written.")


if __name__ == "__main__":
    main()
