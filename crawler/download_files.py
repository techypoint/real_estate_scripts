#!/usr/bin/env python3
"""Download the actual project documents (PDFs) for captured UP-RERA projects.

The uploaded documents are public files on Azure blob storage:
    https://upreradisk.blob.core.windows.net/azureportaldeploy/<file_name>
so no browser / anti-bot is involved -- a plain HTTP GET works.

Input: uprera_details.json exported by the extension (each record's
`detail.documents[]` carries file_name + blob_url).

    python download_files.py --details ~/Downloads/uprera_details.json --out data/files

Files are saved to  <out>/<registration_no>/<file_name>  and existing files are
skipped, so it is safe to re-run.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
from pathlib import Path

try:
    from curl_cffi import requests as http
    SESSION_KW = {"impersonate": "chrome124"}
except ImportError:  # fall back to plain requests (blob is public, works either way)
    import requests as http
    SESSION_KW = {}

BLOB_BASE = "https://upreradisk.blob.core.windows.net/azureportaldeploy/"


def safe(name: str) -> str:
    return re.sub(r'[^A-Za-z0-9._-]+', "_", name).strip("_") or "file"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--details", default="uprera_details.json",
                    help="uprera_details.json exported by the extension")
    ap.add_argument("--out", default="data/files", help="output root folder")
    ap.add_argument("--limit", type=int, default=0, help="stop after N files (0 = all)")
    args = ap.parse_args()

    data = json.load(open(args.details, encoding="utf-8"))
    out_root = Path(args.out)

    session = http.Session(**SESSION_KW)
    total, ok, skipped, failed = 0, 0, 0, 0
    for rec in data:
        reg = rec.get("registration_no") or "unknown"
        docs = (rec.get("detail") or {}).get("documents") or []
        if not docs:
            continue
        folder = out_root / safe(reg)
        folder.mkdir(parents=True, exist_ok=True)
        for d in docs:
            fname = d.get("file_name") or ""
            if not fname:
                continue
            url = d.get("blob_url") or (BLOB_BASE + urllib.parse.quote(fname))
            dest = folder / safe(fname)
            total += 1
            if dest.exists() and dest.stat().st_size > 0:
                skipped += 1
                continue
            try:
                r = session.get(url, timeout=90)
                if r.status_code == 200 and r.content:
                    dest.write_bytes(r.content)
                    ok += 1
                    print(f"  ok   {reg}/{fname}  ({len(r.content)} bytes)")
                else:
                    failed += 1
                    print(f"  FAIL {reg}/{fname}  HTTP {r.status_code}", file=sys.stderr)
            except Exception as e:
                failed += 1
                print(f"  FAIL {reg}/{fname}  {e}", file=sys.stderr)
            if args.limit and ok >= args.limit:
                break
        if args.limit and ok >= args.limit:
            break

    print(f"\nDocuments: {total} listed | {ok} downloaded | {skipped} already present | {failed} failed")
    print(f"Saved under: {out_root}/")


if __name__ == "__main__":
    main()
