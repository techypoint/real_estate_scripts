#!/usr/bin/env python3
"""Merge manually-collected project details into the district project list.

Inputs:
  - data/<district>_projects.json   (from scrape_uprera.py -- the full list)
  - uprera_details.json             (exported by uprera_collector.js in-browser)

Output:
  - data/<district>_merged.json / .csv, joined on registration number.

Registration numbers are normalised (spaces / case / trailing "/MM/YYYY" suffix
variations) before matching, since the list page and the detail page sometimes
format them slightly differently.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path


def norm_reg(reg: str) -> str:
    if not reg:
        return ""
    reg = reg.upper().strip()
    reg = re.sub(r"\s+", "", reg)
    # Match on the core "UPRERAPRJ<digits>" so "/02/2024" style suffixes align.
    m = re.match(r"(UPRERAPRJ\d+)", reg)
    return m.group(1) if m else reg


def load_json(path: Path):
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", required=True, help="data/<district>_projects.json")
    ap.add_argument("--details", default="uprera_details.json",
                    help="exported details JSON from the browser collector")
    ap.add_argument("--out", default="data/merged", help="output path prefix")
    args = ap.parse_args()

    projects = load_json(Path(args.list))
    details = load_json(Path(args.details))

    by_reg: dict[str, dict] = {}
    for d in details:
        by_reg[norm_reg(d.get("registration_no", ""))] = d

    merged = []
    hit = 0
    for p in projects:
        key = norm_reg(p.get("registration_no", ""))
        d = by_reg.get(key)
        row = dict(p)
        if d:
            hit += 1
            row["detail_captured"] = True
            row["detail"] = {
                "basic": d.get("basic", {}),
                "summary": d.get("summary", {}),
                "documents": d.get("documents", []),
                "source_url": d.get("source_url", ""),
            }
        else:
            row["detail_captured"] = False
        merged.append(row)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.with_suffix(".json").open("w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)

    # Flatten a useful subset of detail fields for CSV.
    flat_keys: list[str] = []
    for r in merged:
        for grp in ("basic", "summary"):
            for k in r.get("detail", {}).get(grp, {}):
                col = f"{grp}.{k}"
                if col not in flat_keys:
                    flat_keys.append(col)
    base_cols = ["sno", "registration_no", "project_name", "promoter_name",
                 "district", "project_type", "detail_captured"]
    with out.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(base_cols + flat_keys)
        for r in merged:
            det = r.get("detail", {})
            row = [r.get(c, "") for c in base_cols]
            for col in flat_keys:
                grp, k = col.split(".", 1)
                row.append(det.get(grp, {}).get(k, ""))
            w.writerow(row)

    print(f"Merged {len(merged)} projects; {hit} have collected detail "
          f"({len(merged) - hit} still missing).")
    print(f"  {out.with_suffix('.json')}")
    print(f"  {out.with_suffix('.csv')}")


if __name__ == "__main__":
    main()
