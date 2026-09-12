#!/usr/bin/env python3
"""Parse the UP-RERA "View Project In Detail" (/viewprojects) page.

This is the rich "All Information" page reached from a project's detail page via
the "View Project In Detail" button. It carries far more than the summary:
agents, permits, development works, three bank accounts, land/khasra details, a
big apartment/plan table, registry agreements, and the full document list.

Crucially, every uploaded document is a PUBLIC file on Azure blob storage:
    https://upreradisk.blob.core.windows.net/azureportaldeploy/<uploaded_file_name>
so once we have the file names we can download the actual PDFs directly, no gate.

Usage:
    python parse_viewprojects.py --file "viewprojects.html" --out data/viewproject
    python parse_viewprojects.py --pages-dir data/pages --out data/viewprojects
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import urllib.parse
from pathlib import Path

from bs4 import BeautifulSoup

BLOB_BASE = "https://upreradisk.blob.core.windows.net/azureportaldeploy/"

# scalar fields: element-id suffix -> output key (value text is cleaned of a
# leading "Caption:" prefix when present)
SCALARS = {
    "_lblProjectNameHeading": "project_name",
    "_lblProjectNameWithID": "project_id_raw",
    "_lblregisdate": "registration_date",
    "_lblPromoterNameHeading": "promoter_name",
    "_lblPromoterNameWithID": "promoter_id_raw",
    "_lblProjectType": "project_type",
    "_lblProjectCategory": "project_category",
    "_lblRegistrationFee": "registration_fee",
    "_lblState": "state",
    "_lblProjectDuration": "project_duration_months",
    "_lblProposedStartDate": "proposed_start_date_lbl",
    "_lblrevisedEndDate": "revised_end_date_lbl",
    "_ddlTehsil_old": "tehsil",
}

# data grids: element-id substring -> output key
GRIDS = {
    "grvagents": "agents",
    "grdPermitDetails": "permits",
    "grddevlopmentworks": "development_works",
    "grdCollection": "account_collection",
    "grdSeparate": "account_separate",
    "grdTransaction": "account_transaction",
    "grdLadDetail_doc": "land_documents",   # check the "_doc" one before the bare grid
    "grdLadDetail": "land_details",
    "grd_PlanDetails_ForAdmin": "plan_details",
    "grdKhasra": "khasra",
    "grdRegistryAgreementDetails": "registry_agreements",
}


def _clean(text: str) -> str:
    # "Project Name: Mahagun Medalleo" -> "Mahagun Medalleo"
    return re.sub(r"^[^:]{2,40}:\s*", "", text).strip()


def _rows(table) -> list[dict]:
    """Turn an ASP.NET GridView table into a list of {header: cell} dicts."""
    if table is None:
        return []
    headers = [th.get_text(" ", strip=True) for th in table.find_all("th")]
    out = []
    for tr in table.find_all("tr"):
        if tr.find("th"):
            continue
        tds = tr.find_all("td")
        if not tds:
            continue
        vals = [td.get_text(" ", strip=True) for td in tds]
        if not any(vals):
            continue
        if headers and len(headers) == len(vals):
            out.append({headers[i] or f"col{i}": vals[i] for i in range(len(vals))})
        else:
            out.append({f"col{i}": vals[i] for i in range(len(vals))})
    return out


def parse_viewproject(html: str, source: str = "") -> dict:
    soup = BeautifulSoup(html, "html.parser")
    rec: dict = {"source": source}

    for suffix, key in SCALARS.items():
        el = soup.select_one(f'[id$="{suffix}"]')
        rec[key] = _clean(el.get_text(" ", strip=True)) if el else ""

    # registration number + promoter id from the "(...)" labels
    m = re.search(r"UPRERAPRJ\d+", rec.get("project_id_raw", ""))
    rec["registration_no"] = m.group(0) if m else ""
    m = re.search(r"UPRERAPRM\d+", rec.get("promoter_id_raw", ""))
    rec["promoter_id"] = m.group(0) if m else ""

    # grids
    used_ids: set[str] = set()
    for sub, key in GRIDS.items():
        t = soup.find("table", id=lambda x, s=sub: x and s in x and x not in used_ids)
        if t is not None:
            used_ids.add(t.get("id", ""))
        rec[key] = _rows(t)

    # documents (with direct blob URLs)
    docs = []
    dt = soup.find("table", id=lambda x: x and "grvdocumentdetails" in x)
    if dt is not None:
        headers = [th.get_text(" ", strip=True) for th in dt.find_all("th")]
        # find the "Uploaded File Name" column index
        fname_idx = next((i for i, h in enumerate(headers) if "file name" in h.lower()), 2)
        name_idx = next((i for i, h in enumerate(headers) if h.lower() == "document name"), 1)
        date_idx = next((i for i, h in enumerate(headers) if "date" in h.lower()), None)
        type_idx = next((i for i, h in enumerate(headers) if "doc type" in h.lower() or "upload doc" in h.lower()), None)
        for tr in dt.find_all("tr"):
            if tr.find("th"):
                continue
            tds = tr.find_all("td")
            if len(tds) <= fname_idx:
                continue
            fname = tds[fname_idx].get_text(" ", strip=True)
            if not fname:
                continue
            docs.append({
                "document_name": tds[name_idx].get_text(" ", strip=True) if len(tds) > name_idx else "",
                "file_name": fname,
                "uploaded_date": tds[date_idx].get_text(" ", strip=True) if date_idx is not None and len(tds) > date_idx else "",
                "upload_type": tds[type_idx].get_text(" ", strip=True) if type_idx is not None and len(tds) > type_idx else "",
                "blob_url": BLOB_BASE + urllib.parse.quote(fname),
            })
    rec["documents"] = docs

    # internal form id, from a document filename (PRJ..<formid>..) or map link
    form_id = ""
    a = soup.select_one('a[href*="mapview_project.aspx?form="]')
    if a and a.has_attr("href"):
        mm = re.search(r"form=(\d+)", a["href"])
        if mm:
            form_id = mm.group(1)
    if not form_id and docs:
        mm = re.search(r"PRJ\d+?(\d{5,})", docs[0]["file_name"])
        # best-effort: the trailing digits after the PRJ<docid> chunk
    rec["form_id"] = form_id

    return rec


def is_shell(rec: dict) -> bool:
    return not rec.get("registration_no") and not rec.get("project_name")


def main() -> None:
    ap = argparse.ArgumentParser()
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--file")
    src.add_argument("--pages-dir")
    ap.add_argument("--out", default="data/viewprojects")
    args = ap.parse_args()

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
        rec = parse_viewproject(p.read_text(encoding="utf-8", errors="replace"), source=p.name)
        if is_shell(rec):
            shells += 1
            print(f"  SKIP (blank/not a viewprojects page): {p.name}", file=sys.stderr)
            continue
        records.append(rec)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.with_suffix(".json").open("w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    cols = ["registration_no", "project_name", "promoter_name", "promoter_id",
            "registration_date", "project_type", "project_category", "registration_fee",
            "project_duration_months", "state", "tehsil", "document_count",
            "agent_count", "plan_row_count", "khasra_count", "source"]
    with out.with_suffix(".csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = dict(r)
            row["document_count"] = len(r.get("documents", []))
            row["agent_count"] = len(r.get("agents", []))
            row["plan_row_count"] = len(r.get("plan_details", []))
            row["khasra_count"] = len(r.get("khasra", []))
            w.writerow(row)

    # a flat file manifest for bulk download
    with (out.parent / "file_manifest.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["registration_no", "project_name", "document_name", "file_name", "uploaded_date", "blob_url"])
        for r in records:
            for d in r.get("documents", []):
                w.writerow([r.get("registration_no", ""), r.get("project_name", ""),
                            d["document_name"], d["file_name"], d["uploaded_date"], d["blob_url"]])

    print(f"Parsed {len(records)} viewproject pages ({shells} skipped).")
    print(f"  {out.with_suffix('.json')}")
    print(f"  {out.with_suffix('.csv')}")
    print(f"  {out.parent / 'file_manifest.csv'}")


if __name__ == "__main__":
    main()
