# Project brochures — handoff

Status as of 2026-10-19. Goal: fill each project's content (`projectcontents`,
see `schema/project-schema.json`) from the builder's brochure. Step 1 — getting
the brochure — is built (`brochure_finder.py`). Step 2 — turning a brochure
into a `data/content/<REG>.json` — is next.

## Why it's built this way

- **UP-RERA has no brochure.** Its document list is legal/technical (approvals,
  certificates, sanctioned plans, floor plans). Builders occasionally drop a
  brochure under "Other Plan (If Any)", but rarely.
- **No search API.** We don't have one, so brochures come from the **builder's
  own website** — the only open, legitimate source. Portals (99acres,
  MagicBricks, Housing) gate brochures behind login/OTP and forbid scraping.
- **Project-first, not crawl-everything.** For one project we go straight to
  its page on the builder site and take that page's brochure. (An earlier
  version listed every PDF on a site and matched afterwards — dropped.)
- **RERA promoter ≠ brand.** 1,419 projects come from ~800 promoter companies,
  mostly one SPV per project. Promoter names are grouped into ~600 brands
  (`Gaursons Realtech Pvt Ltd` → `gaursons`), and a brand maps to one website.

## How `brochure_finder.py find` works (per project)

Input: projects in Mongo with `detail_fetch_status: "done"` and no
`brochure_status` yet, one at a time.

1. **Builder website** — promoter → brand → domain from
   `crawler/data/brand_domains.csv`. No domain → `brochure_status: no_domain`.
2. **Project page** on that site, no search engine needed:
   - WordPress sites: `/wp-json/wp/v2/media?search=<name>&mime_type=application/pdf`
     (often returns the brochure PDF directly) and `/wp-json/wp/v2/search`.
   - Otherwise the `sitemap.xml` URL whose slug matches the project name.
     Numbers must agree ("7th Avenue" never matches "14th-avenue").
3. **Brochure on that page** — `.pdf` / "Download Brochure" links in the HTML;
   headless Chromium (Playwright) only if the page is JS-rendered or the
   brochure is behind a button (click → catch download/new tab/PDF request).
   A click that opens a **lead form** → `brochure_status: gated`. We never
   submit fake enquiries.
4. **Verify + save** — download that one PDF; accept if its text contains the
   RERA number (`high`) or all the project's name words (`medium`), or the
   builder named the file `<project>…brochure` (`medium`, for image-only PDFs).
   Upload to R2 and write Mongo; otherwise `brochure_status: not_found`.

No AI/LLM anywhere in this step — all rule-based. Running cost ≈ R2 storage only.

## Where things are saved

R2: `brochures/<registration_no>/<file_name>` (next to `documents/<reg>/`).

Mongo `projects` document:

```js
brochure: {
  url: "brochures/UPRERAPRJ918/Allure_Brochurer_.pdf",  // R2 key
  file_name, source_url, page_url,
  found_via: "wp-media" | "page" | "page-js" | "browser-download" | ...,
  size, pages, match_score,
  confidence: "high" | "medium",
  fetched_at
},
brochure_status: "found" | "not_found" | "gated" | "no_domain",
brochure_checked_at
```

## Commands

```bash
cd /home/deepak/projects/real_estate_scripts
source .venv/bin/activate
pip install -r crawler/requirements.txt && python -m playwright install chromium   # once
cd crawler

python brochure_finder.py find --dry-run                 # look up only, write nothing
python brochure_finder.py find                           # real run → R2 + Mongo
python brochure_finder.py find --limit 20
python brochure_finder.py find --district "Gautam Buddha Nagar"
python brochure_finder.py find --reg UPRERAPRJ918 -v     # one project, show pages tried
python brochure_finder.py find --force                   # re-check already-checked projects
python brochure_finder.py find --no-browser              # skip headless Chromium

python brochure_finder.py domains --limit 100            # guess sites for next 100 unlooked-up brands
python brochure_finder.py domains --retry                # retry brands where nothing was found
```

## `crawler/data/brand_domains.csv`

Columns: `brand, domain, status, projects, homepage_title, score, promoters`.

| status | meaning |
|---|---|
| `manual` | set by a human — never overwritten by `domains` |
| `verified` | script found the homepage names one of the brand's projects — spot-check |
| `auto` | plausible guess, unconfirmed — **review** |
| `none` | nothing found, or a namesake that was cleared |

Top 100 brands (754 projects): 25 manual, 6 verified, 14 auto, 55 none.
Auto-guessing alone is unreliable (it found Imperia *Pasta*, a Texas CBS, a
Utah "Home Realty"), so the top builders were checked by hand.

To fix a row: set `domain`, set `status=manual`. When a project is registered
under an SPV with an unrelated name (Mahagun Mirabella → "Golfgreen"), add/edit
that brand's row to point at the parent builder's site.

## Test results (dry run)

Found correctly: Gaur City 7th Avenue, Gaur City 14th Avenue, Gaur City Center,
Gaurs Runway Suites (Gaursons — plain page links); Mahagun Mywoods (WP media);
ATS Destinaire, ATS Kabana High, ATS Allure.
Correctly not found: 2nd Parkview (old delivered project), ATS Bouquet,
ATS Kinghood Drive; Prateek Edifice (only a corporate brochure — rejected).

## Known gaps / TODO

- Only **2** projects have `detail_fetch_status: "done"` so far (ATS Allure,
  2nd Parkview) — coverage grows as `process_details.py` runs.
- **Supertech** (35 projects): site has no sitemap and JS-rendered pages; needs
  a site-specific page finder.
- 14 `auto` rows in the top 100 need review — esp. `adani` (42 projects, really
  the old Jaiprakash/Jaypee Greens projects; is `adanirealty.com` right?).
- Old/delivered projects and small one-project builders mostly have no brochure
  online → fall back to RERA floor/layout plan PDFs for content.
- Nothing has been written to R2/Mongo yet — only dry runs.

## Step 2: brochure → project content (`brochure_content.py`, uses AI)

Built 2026-10-19. Per project with `brochure_status: "found"` (and no
`brochure_content_status`): download the PDF from R2 → render pages with
`pdftoppm` (brochures are often image-only, no text) → `claude -p` (Claude Code
CLI, Read tool only, default model opus) reads every page + the schema →
returns content + picture boxes → script crops pictures into
`data/media/<REG>/` (`.webp` + `-thumb.webp`) → writes
`data/content/<REG>.json` (`published: false`) → runs the validator.

Brochure only: no RERA merge, no pricing unless printed. Every unit carries
`source_ref.page`; the AI's doubts print as `check:` lines. Never overwrites an
existing content file without `--force`. Work files (PDF, renders, raw Claude
reply) in `crawler/data/brochure_work/<REG>/`.

```bash
python brochure_content.py run --reg UPRERAPRJ918
python brochure_content.py run --limit 5
python brochure_content.py run --reg UPRERAPRJ918 --reuse   # re-crop from saved reply, no AI call
python brochure_content.py run --limit 5 --no-import       # files only, no R2/Mongo
```

One step: if validation passes, the script uploads `data/media/<REG>/` to R2
and runs `import_content.mjs <REG>` — imported as `published: false` (an
existing file's `published` value is kept). To publish: set `"published": true`
in the JSON, then `npm run import:content -- <REG>`.

Mongo: `brochure_content_status` (extracted|failed), `brochure_content_at`,
`brochure_content_valid`, `brochure_content_cost_usd`, `brochure_content_error`.

First result, ATS Allure: 82 s, ~$0.87 API-equivalent, 2 unit types, specs,
amenities, club, 10 landmarks, 8 images. Validation passes (70%).

Schema change (2026-10-19): pricing.* and marketing.city / status /
possession_on and scale.land_area went from required to `optional` — brochures
rarely print them and pricing isn't shown on the site. All four exist in RERA
(district, project_type, declared_completion_date, khasra area) if ever wanted.
