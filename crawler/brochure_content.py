#!/usr/bin/env python3
"""Turn a project's brochure (found by brochure_finder.py) into its content file.

Step 2 of the brochure pipeline. Per project with brochure_status=found:

  1. download the brochure PDF from R2 (brochures/<reg>/...)
  2. render every page to an image (pdftoppm) — most brochures are image-only,
     so there is no text to extract; the pages have to be *looked at*
  3. run the Claude Code CLI (`claude -p`, read-only: it may only use Read) on
     the page images + schema/project-schema.json. It copies only what is
     printed, cites the page of every fact, and says where the useful pictures
     are (gallery, site plan, location map, floor plans) as boxes on a page
  4. crop those pictures from a high-res render into data/media/<reg>/
     (<name>.webp + <name>-thumb.webp, the same layout as UPRERAPRJ125561)
  5. write data/content/<reg>.json (published: false) and run the validator
  6. if it passes: upload data/media/<reg>/ to R2 and import it into
     projectcontents (importers/import_content.mjs) — still unpublished

Brochure only — nothing is merged in from the RERA record, and pricing is left
empty unless the brochure prints it. A person reviews the page, then sets
"published": true in the JSON and re-runs `npm run import:content -- <REG>`.

Usage:
    python brochure_content.py run --reg UPRERAPRJ918
    python brochure_content.py run --limit 5
    python brochure_content.py run --reg UPRERAPRJ918 --reuse   # re-crop, no new AI call
    python brochure_content.py run --limit 5 --no-import       # files only, no R2/Mongo

Needs `pdftoppm` (poppler-utils), `pip install Pillow`, and `claude` on PATH.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image

from uprera_store import COL_PROJECTS, ROOT, connect_mongo, load_env, r2_client, r2_put_object, required_env

WORK = Path(__file__).resolve().parent / "data" / "brochure_work"
CONTENT_DIR = ROOT / "data" / "content"
MEDIA_DIR = ROOT / "data" / "media"
SCHEMA = ROOT / "schema" / "project-schema.json"
EXAMPLE = CONTENT_DIR / "UPRERAPRJ125561.json"

AI_PX = 1568       # long edge of the copies Claude reads (it downsizes anything bigger)
CROP_DPI = 200     # render used for the saved images
THUMB_W = 480      # matches the existing *-thumb.webp files
CLAUDE_TIMEOUT = 1200
IMAGE_USES = {"gallery", "master_plan", "location_map", "plan_2d", "club"}

PROMPT = """You are filling the content file for one real-estate project from its builder brochure.

Project: {name} (UP-RERA {reg}), promoter {promoter}.
Brochure: {pages} pages, rendered as images:
{page_list}

Do this:
1. Read schema/project-schema.json. It defines every field; its x-extraction notes say where in a
   brochure each field is usually found, and its enums are the only allowed values.
2. Glance at data/content/UPRERAPRJ125561.json — a finished file for another project. Copy its
   shape and style only, never its values.
3. Read EVERY page image above, one by one.

Rules — the output is published after a human check, so it must be traceable:
- Only values actually printed in this brochure. If something is not printed, leave the field out.
  No guessing, no outside knowledge, no values from the RERA record.
- Numbers exactly as printed. Areas: put each printed figure in the unit it was printed in
  (sqft or sqm) — do not convert. "Total Area" / "Super Area" is areas.super.
- No pricing section unless the brochure prints prices.
- connectivity: only landmarks the brochure names; minutes/distance_km only if printed.
- Every unit_types entry gets source_ref {{"source_id": "brochure", "page": <page no>, "confidence": "high"|"medium"|"low"}};
  use "low" with a "note" when the print was hard to read.
- Skip pages about the builder's other projects, the company profile and disclaimers.
- Leave out registration_no, slug, published, sources, gallery, master_plan, location_map and every
  media / url field — the script fills those. Instead list the pictures in "images" (below).

"images": the pictures worth publishing — project renders/photos (gallery), the site/master plan
(master_plan), the location map (location_map), each unit's floor plan (plan_2d, with unit_key =
that unit_types key), and club photos (club, with club = that clubs[].name). Not logos, not other
projects, not decorative flowers. "box" is [left, top, right, bottom] as fractions 0..1 of the
page image, tight around the picture (exclude headings and page text); use [0,0,1,1] only for a
truly full-page picture.

Reply with ONE JSON object and nothing else:
{{
  "content": {{ "marketing": {{...}}, "scale": {{...}}, "unit_types": [...], "amenities": [...],
               "clubs": [...], "connectivity": [...], "specifications": [...] }},
  "images": [ {{"page": 6, "box": [0.05, 0.08, 0.95, 0.92], "use": "master_plan",
               "name": "site-plan", "label": "Site Plan", "alt": "..."}} ],
  "page_notes": [ {{"page": 1, "used_for": "marketing"}}, {{"page": 12, "skipped": "other ATS projects"}} ],
  "unsure": [ "anything a reviewer should double-check" ]
}}
"""


def now() -> datetime:
    return datetime.now(timezone.utc)


def slugify(*parts: str) -> str:
    text = " ".join(p for p in parts if p)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


def pending_projects(db, args) -> list[dict]:
    """Projects with a stored brochure and no content extracted yet.
    --force redoes ones already extracted."""
    query: dict = {"brochure_status": "found"}
    if args.reg:
        query = {"registration_no": args.reg}
    elif not args.force:
        query["brochure_content_status"] = {"$exists": False}
    fields = {"registration_no": 1, "project_name": 1, "promoter_name": 1, "brochure": 1}
    cursor = db[COL_PROJECTS].find(query, fields).sort([("district", 1), ("sno", 1)])
    if args.limit:
        cursor = cursor.limit(args.limit)
    return list(cursor)


def render_pages(pdf: Path, out: Path) -> tuple[list[Path], list[Path]]:
    """Two renders: small copies for Claude to read, high-res ones to crop from."""
    ai_dir, hi_dir = out / "pages", out / "hires"
    for d in (ai_dir, hi_dir):
        shutil.rmtree(d, ignore_errors=True)
        d.mkdir(parents=True)
    subprocess.run(["pdftoppm", "-png", "-scale-to", str(AI_PX), str(pdf), str(ai_dir / "page")], check=True)
    subprocess.run(["pdftoppm", "-png", "-r", str(CROP_DPI), str(pdf), str(hi_dir / "page")], check=True)
    return sorted(ai_dir.glob("page-*.png")), sorted(hi_dir.glob("page-*.png"))


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < 0:
        raise ValueError("no JSON object in Claude's reply")
    return json.loads(text[start:end + 1])


def ask_claude(proj: dict, pages: list[Path], model: str) -> tuple[dict, dict]:
    page_list = "\n".join(f"  page {i}: {p.relative_to(ROOT)}" for i, p in enumerate(pages, 1))
    prompt = PROMPT.format(name=proj.get("project_name"), reg=proj["registration_no"],
                           promoter=proj.get("promoter_name"), pages=len(pages), page_list=page_list)
    cmd = ["claude", "-p", "--output-format", "json", "--model", model,
           "--tools", "Read", "--allowedTools", "Read", "--no-session-persistence"]
    proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=ROOT, timeout=CLAUDE_TIMEOUT)
    if proc.returncode != 0:
        raise RuntimeError(f"claude exited {proc.returncode}: {(proc.stderr or proc.stdout)[-800:]}")
    run = json.loads(proc.stdout)
    if run.get("is_error"):
        raise RuntimeError(f"claude error: {run.get('result')}")
    return parse_json(run.get("result") or ""), run


def crop_images(images: list[dict], hires: list[Path], reg: str) -> list[tuple[dict, dict]]:
    """Cut each listed picture out of its high-res page; returns (spec, media_ref) pairs."""
    out_dir = MEDIA_DIR / reg
    out_dir.mkdir(parents=True, exist_ok=True)
    done, used_names = [], set()
    for spec in images:
        page, box = spec.get("page"), spec.get("box") or [0, 0, 1, 1]
        if spec.get("use") not in IMAGE_USES or not isinstance(page, int) or not 1 <= page <= len(hires):
            print(f"    skip image {spec.get('name')!r}: bad use/page")
            continue
        with Image.open(hires[page - 1]) as im:
            w, h = im.size
            l, t, r, b = (max(0.0, min(1.0, float(v))) for v in box)
            if r - l < 0.05 or b - t < 0.05:
                print(f"    skip image {spec.get('name')!r}: box too small {box}")
                continue
            crop = im.convert("RGB").crop((round(l * w), round(t * h), round(r * w), round(b * h)))

        name = slugify(spec.get("name") or spec.get("label") or f"page-{page}") or f"page-{page}"
        while name in used_names:
            name += "-2"
        used_names.add(name)
        crop.save(out_dir / f"{name}.webp", "WEBP", quality=85)
        thumb = crop.copy()
        thumb.thumbnail((THUMB_W, THUMB_W * 10))
        thumb.save(out_dir / f"{name}-thumb.webp", "WEBP", quality=80)

        ref = {"url": f"media/{reg}/{name}.webp", "label": spec.get("label") or name,
               "alt": spec.get("alt") or spec.get("label") or name,
               "width": crop.width, "height": crop.height}
        done.append((spec, ref))
    return done


def build_content(proj: dict, reply: dict, crops: list[tuple[dict, dict]]) -> dict:
    reg = proj["registration_no"]
    body = reply.get("content") or {}
    brochure = proj["brochure"]
    as_of = brochure.get("fetched_at")
    marketing = body.get("marketing") or {}

    doc: dict = {
        "registration_no": reg,
        "slug": slugify(marketing.get("display_name") or proj.get("project_name"),
                        marketing.get("locality"), marketing.get("city")),
        "published": False,
    }
    for key in ("marketing", "scale", "pricing", "unit_types", "amenities", "clubs", "connectivity", "specifications"):
        if body.get(key):
            doc[key] = body[key]

    units = {u.get("key"): u for u in doc.get("unit_types", [])}
    clubs = {c.get("name"): c for c in doc.get("clubs", [])}
    gallery, master_plan, location_map = [], [], []
    for spec, ref in crops:
        use = spec["use"]
        if use == "plan_2d" and spec.get("unit_key") in units:
            units[spec["unit_key"]].setdefault("media", {}).setdefault("plan_2d", []).append(ref)
        elif use == "club" and spec.get("club") in clubs:
            clubs[spec["club"]].setdefault("media", []).append(ref)
        elif use == "master_plan":
            master_plan.append(ref)
        elif use == "location_map":
            location_map.append(ref)
        else:  # gallery, or a plan/club photo whose unit/club didn't match
            gallery.append({**ref, "order": len(gallery) + 1})
    if gallery:
        doc["gallery"] = gallery
    if master_plan:
        doc["master_plan"] = master_plan
    if location_map:
        doc["location_map"] = location_map

    doc["sources"] = [{
        "id": "brochure",
        "label": f"{marketing.get('display_name') or proj.get('project_name')} builder brochure",
        "type": "brochure",
        **({"as_of": as_of.strftime("%Y-%m-%d")} if isinstance(as_of, datetime) else {}),
        "url": brochure["url"],
    }]
    doc["updated_by"] = "brochure_content.py"
    return doc


def validate(reg: str) -> bool:
    proc = subprocess.run(["node", str(ROOT / "validate_content.mjs"), reg], cwd=ROOT)
    return proc.returncode == 0


def upload_media(s3, bucket: str, reg: str) -> int:
    """Push data/media/<reg>/ to R2 at the same keys the JSON references
    (media/<reg>/<file>). Overwrites, so a --reuse re-crop replaces old images."""
    files = sorted((MEDIA_DIR / reg).glob("*.webp"))
    for f in files:
        r2_put_object(s3, bucket, f"media/{reg}/{f.name}", f.read_bytes(), "image/webp", f.name)
    return len(files)


def import_content(reg: str) -> bool:
    proc = subprocess.run(["node", str(ROOT / "importers" / "import_content.mjs"), reg], cwd=ROOT)
    return proc.returncode == 0


def save_status(db, reg: str, status: str, extra: dict | None = None) -> None:
    db[COL_PROJECTS].update_one({"registration_no": reg}, {"$set": {
        "brochure_content_status": status, "brochure_content_at": now(), **(extra or {})}})


def cmd_run(db, args) -> None:
    for tool in ("pdftoppm", "claude", "node"):
        if not shutil.which(tool):
            raise SystemExit(f"`{tool}` not found on PATH.")

    todo = pending_projects(db, args)
    print(f"{len(todo)} project(s) to extract.")
    env = load_env()
    s3, bucket = r2_client(env), required_env(env, "R2_BUCKET")
    stats = {"extracted": 0, "imported": 0, "failed": 0, "skipped": 0}

    for i, proj in enumerate(todo, 1):
        reg = proj["registration_no"]
        print(f"[{i}/{len(todo)}] {reg} {proj.get('project_name')}")
        if not proj.get("brochure", {}).get("url"):
            print("    no brochure stored — run brochure_finder.py first")
            stats["skipped"] += 1
            continue
        target = CONTENT_DIR / f"{reg}.json"
        if target.exists() and not args.force and not args.reuse:
            print(f"    {target.relative_to(ROOT)} already exists — use --force to overwrite")
            stats["skipped"] += 1
            continue

        work = WORK / reg
        work.mkdir(parents=True, exist_ok=True)
        reply_file = work / "claude_reply.json"
        try:
            if args.reuse and reply_file.exists():
                reply = json.loads(reply_file.read_text())
                hires = sorted((work / "hires").glob("page-*.png"))
                cost = None
                print("    reusing the saved Claude reply")
            else:
                pdf = work / "brochure.pdf"
                s3.download_file(bucket, proj["brochure"]["url"], str(pdf))
                pages, hires = render_pages(pdf, work)
                print(f"    {len(pages)} pages rendered; asking Claude ({args.model}) — takes a few minutes")
                reply, run = ask_claude(proj, pages, args.model)
                reply_file.write_text(json.dumps(reply, indent=2, ensure_ascii=False))
                (work / "claude_run.json").write_text(json.dumps(run, indent=2))
                cost = run.get("total_cost_usd")
                print(f"    Claude done in {run.get('duration_ms', 0) / 1000:.0f}s, "
                      f"{run.get('num_turns')} turns, ${cost or 0:.2f} (API-equivalent)")

            crops = crop_images(reply.get("images") or [], hires, reg)
            doc = build_content(proj, reply, crops)
            if target.exists():  # never unpublish a page someone already published
                doc["published"] = bool(json.loads(target.read_text()).get("published"))
            CONTENT_DIR.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
            sections = [k for k in doc if k not in ("registration_no", "slug", "published", "updated_by")]
            print(f"    wrote {target.relative_to(ROOT)}: {', '.join(sections)}; {len(crops)} image(s)")
            for note in reply.get("unsure") or []:
                print(f"    check: {note}")

            valid = validate(reg)
            status = "extracted"
            if valid and not args.no_import:
                print(f"    uploaded {upload_media(s3, bucket, reg)} image(s) to R2")
                if not import_content(reg):
                    raise RuntimeError("import_content.mjs failed")
                status = "imported"
                stats["imported"] += 1
            elif not valid:
                print("    not imported — fix the validation errors, then re-run with --reuse")
            stats["extracted"] += 1
            save_status(db, reg, status, {"brochure_content_valid": valid,
                                          **({"brochure_content_cost_usd": cost} if cost is not None else {})})
        except Exception as e:  # one bad brochure must not stop the batch
            stats["failed"] += 1
            print(f"    FAILED: {e}")
            save_status(db, reg, "failed", {"brochure_content_error": str(e)[:500]})

    print(f"\nDone. {stats['extracted']} extracted ({stats['imported']} imported), "
          f"{stats['failed']} failed, {stats['skipped']} skipped.")
    if stats["imported"]:
        print('To publish: review the page, set "published": true in data/content/<REG>.json, '
              "then npm run import:content -- <REG>")


def main() -> None:
    ap = argparse.ArgumentParser(description="Brochure -> data/content/<REG>.json via the Claude Code CLI.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="extract content from each stored brochure")
    r.add_argument("--reg", help="just this registration number")
    r.add_argument("--limit", type=int)
    r.add_argument("--model", default="opus", help="claude model alias (default: opus)")
    r.add_argument("--force", action="store_true", help="redo extracted projects / overwrite existing JSON")
    r.add_argument("--reuse", action="store_true", help="rebuild JSON + crops from the saved Claude reply")
    r.add_argument("--no-import", action="store_true", help="only write the files; skip R2 upload + Mongo import")

    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)  # keep our lines in order with node's output
    client, db = connect_mongo(load_env())
    try:
        {"run": cmd_run}[args.cmd](db, args)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(main())
