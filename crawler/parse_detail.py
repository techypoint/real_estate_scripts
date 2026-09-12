#!/usr/bin/env python3
"""Parse UP-RERA project-detail HTML (the 'Projectsummary' page) into structured data.

The detail page renders full data only inside a genuine, human-driven browser
session (automated clients get a blank shell). So the intended workflow is:

  1. You open each project's detail page in your normal browser and save it
     (Ctrl+S -> "Webpage, HTML only") into a folder, e.g. data/pages/.
     -- or export via the in-browser collector (uprera_collector.js) instead.
  2. Run this parser over the folder to produce one clean CSV/JSON dataset.

    python parse_detail.py --pages-dir data/pages --out data/details

Field extraction is keyed on the stable element IDs in the page
(id$="_lblprojectname", etc.), so it is robust to the ASP.NET id prefix and to
the encrypted query-string in the page URL.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

from bs4 import BeautifulSoup

# suffix of element id  ->  output field name
FIELD_MAP = {
    "_lblprojectname": "project_name",
    "_lblregno": "registration_no",
    "_prjregdate": "registration_date",
    "_prj_type": "project_type",
    "_contactnumber": "coordinator_number",
    "_Lblpropeseperoid": "proposed_period_months",
    "_lblpropesedDatestart": "proposed_start_date",
    "_Lblproposedenddt": "declared_completion_date",
    "_promoterMobileNumber": "promoter_mobile_contactbox",
    "_projectCordinatorNumber": "coordinator_number_contactbox",
    "_helpLineNumber": "helpline_number",
    "_lblState": "state",
    "_lbldistrict": "district",
    "_lbltechsil": "tehsil",
    "_lblpaddress": "project_address",
    "_Lblpromotername": "promoter_name",
    "_LblAppicanttype": "promoter_applicant_type",
    "_Lblmobilenumber": "promoter_mobile",
    "_Lblemailapp": "promoter_email",
    "_Lbladdressapplicant": "promoter_address",
    "_lblpromoterChairmanaddress": "promoter_chairman_address",
    "_lbltotalproject": "promoter_total_projects",
    "_lbltotalcomplaint": "promoter_total_complaints",
    "_lblprojectwisecomplaint": "project_complaints",
}


def _text(el) -> str:
    return el.get_text(" ", strip=True) if el else ""


def parse_detail(html: str, source: str = "") -> dict:
    soup = BeautifulSoup(html, "html.parser")
    rec: dict = {"source": source}

    for suffix, key in FIELD_MAP.items():
        rec[key] = _text(soup.select_one(f'[id$="{suffix}"]'))

    # Internal numeric project id, from the "View On Map" link / QR image.
    form_id = ""
    a = soup.select_one('a[href*="mapview_project.aspx?form="]')
    if a and a.has_attr("href"):
        m = re.search(r"form=(\d+)", a["href"])
        if m:
            form_id = m.group(1)
    if not form_id:
        img = soup.select_one('[id$="_imgQRCode"]')
        if img and img.has_attr("src"):
            m = re.search(r"_(\d+)\.png", img["src"])
            if m:
                form_id = m.group(1)
    rec["form_id"] = form_id

    # Co-promoters (grd_multiple): one block per ctlNN row.
    co = []
    grid = soup.select_one('[id*="grd_multiple"]')
    if grid:
        ctls = sorted({m.group(1)
                       for el in grid.select('[id*="grd_multiple_ctl"]')
                       for m in [re.search(r"grd_multiple_(ctl\d+)_", el.get("id", ""))]
                       if m})
        for ctl in ctls:
            def gc(suffix):
                return _text(grid.select_one(f'[id$="grd_multiple_{ctl}{suffix}"]'))
            entry = {
                "name": gc("_lblname"),
                "applicant_type": gc("_lbl_apptype"),
                "mobile": gc("_lblmobile"),
                "email": gc("_lbl_email"),
                "address": gc("_lbl_prm_address"),
            }
            if any(entry.values()):
                co.append(entry)
    rec["co_promoters"] = co

    # Document details (grvdocumentdetails): SNo / name / upload-type / doc id.
    docs = []
    dgrid = soup.select_one('[id*="grvdocumentdetails"]')
    if dgrid:
        ctls = sorted({m.group(1)
                       for el in dgrid.select('[id*="grvdocumentdetails_ctl"]')
                       for m in [re.search(r"grvdocumentdetails_(ctl\d+)_", el.get("id", ""))]
                       if m},
                      key=lambda c: int(re.search(r"\d+", c).group()))
        for ctl in ctls:
            def gd(suffix):
                el = dgrid.select_one(f'[id$="grvdocumentdetails_{ctl}{suffix}"]')
                if el is None:
                    return ""
                return el.get("value", "") if el.name == "input" else _text(el)
            name = gd("_Label2")
            if not name:
                continue
            docs.append({
                "sno": gd("_lblSRNO"),
                "name": name,
                "upload_type": gd("_Labdoc"),
                "doc_id": gd("_mainid"),
            })
    rec["documents"] = docs

    return rec


def is_shell(rec: dict) -> bool:
    """The bot-gate 'shell' page has all the data labels blank."""
    return not rec.get("registration_no") and not rec.get("project_name")


def main() -> None:
    ap = argparse.ArgumentParser(description="Parse saved UP-RERA detail pages.")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--pages-dir", help="folder of saved .html detail pages")
    src.add_argument("--file", help="a single .html detail page")
    ap.add_argument("--out", default="data/details", help="output path prefix")
    args = ap.parse_args()

    paths: list[Path]
    if args.file:
        paths = [Path(args.file)]
    else:
        d = Path(args.pages_dir)
        paths = sorted(list(d.glob("*.html")) + list(d.glob("*.htm")))
    if not paths:
        print("No HTML files found.", file=sys.stderr)
        sys.exit(1)

    records, shells = [], 0
    for p in paths:
        rec = parse_detail(p.read_text(encoding="utf-8", errors="replace"), source=p.name)
        if is_shell(rec):
            shells += 1
            print(f"  SKIP (blank shell): {p.name}", file=sys.stderr)
            continue
        records.append(rec)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.with_suffix(".json").open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    scalar_cols = ["source", "registration_no", "project_name", "form_id",
                   "registration_date", "project_type", "proposed_period_months",
                   "proposed_start_date", "declared_completion_date",
                   "state", "district", "tehsil", "project_address",
                   "coordinator_number", "helpline_number",
                   "promoter_name", "promoter_applicant_type", "promoter_mobile",
                   "promoter_email", "promoter_address", "promoter_chairman_address",
                   "promoter_total_projects", "promoter_total_complaints",
                   "project_complaints", "co_promoter_count", "document_count"]
    with out.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=scalar_cols, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = dict(r)
            row["co_promoter_count"] = len(r.get("co_promoters", []))
            row["document_count"] = len(r.get("documents", []))
            w.writerow(row)

    print(f"Parsed {len(records)} detail pages ({shells} blank shells skipped).")
    print(f"  {out.with_suffix('.json')}")
    print(f"  {out.with_suffix('.csv')}")


if __name__ == "__main__":
    main()
