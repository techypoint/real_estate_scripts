# real_estate_scripts

Scrapers, importers, brochure/media ingest, and crons for the UP RERA Estates
project. See the repo-root `CLAUDE.md` (one level up, in `real_estate_project/`)
for the full picture of how this fits with `real_estate_backend` and
`real_estate_frontend`.

```
crawler/    Python scrapers + Chrome extension/userscript that pull data
            from up-rera.in (list, per-project detail, document PDFs)
importers/  import_rera.mjs, import_content.mjs — load crawler/curated
            output into Mongo
lib/        db.mjs (Mongo access), r2.mjs (Cloudflare R2 client)
schema/     project-schema.json — the master field definition (see CLAUDE.md)
data/       Raw scrape JSON/CSV, curated data/content/*.json, data/media/
validate_content.mjs   Validates data/content/*.json against schema/
migrate_to_r2.mjs       One-off cutover script (see docs at repo root)
```

---

## Data collection (`crawler/`)

### Key findings (why it's built this way)

1. **The list is open.** `frm_allprojectdistrictwise.aspx?districtname=<D>` is
   a plain server-rendered page; a single GET returns every project for a
   district. Standard UP district names work (Gautam Buddha Nagar 1,119;
   Lucknow 897; Ghaziabad 520; …).

2. **The detail pages are anti-bot gated.** `View_Registration_Details.aspx`
   and the newer `Projectsummary` page render a blank "shell" (all fields
   empty) to every automated client — `requests`, `curl`, `curl_cffi` with
   Chrome TLS, and Playwright (headless *and* headful, real Chrome binary,
   real clicks, anti-webdriver). They render real data **only inside a
   genuine, human-driven browser session**. Scripted project-selection is
   also ignored (the page shows whatever project is in the session), so
   unattended crawling is not possible. → Solution: a browser **extension**
   that rides along with normal browsing and captures whatever detail page
   you open.

3. **The document files are NOT gated.** Every uploaded document is a public
   object on Azure blob storage:
   `https://upreradisk.blob.core.windows.net/azureportaldeploy/<file_name>`
   — a plain GET returns the PDF. So once filenames are captured (from the
   `View Project In Detail` page), all documents can be downloaded freely.

### Python scripts

| File | Purpose |
|---|---|
| `scrape_uprera.py` | Scrape a district's full project **list** → `../data/<district>_projects.{csv,json}`. |
| `parse_detail.py` | Parse a saved **summary** detail page (`.html`) → structured JSON/CSV. |
| `parse_viewprojects.py` | Parse a saved **View Project In Detail** page → JSON/CSV + `file_manifest.csv` (with blob URLs). |
| `download_files.py` | Download all document PDFs referenced in an exported `uprera_details.json` → `../data/files/<reg_no>/`. |
| `merge_details.py` | Join collected details to a district list on registration number. |
| `requirements.txt` | `requests`, `beautifulsoup4` (also uses `curl_cffi` when available). Installed into `.venv/`. |

### Chrome extension — `crawler/uprera_extension/`

| File | Role |
|---|---|
| `manifest.json` | MV3 config: runs on all `up-rera.in` pages; `storage` + `downloads` perms; background worker; blob host permission. |
| `common.js` | Shared field map, CSV builder, `chrome.storage` helpers. |
| `content.js` | Auto-captures summary + `viewprojects` pages (merged per project); floating tab → sidebar UI (Shadow DOM). |
| `background.js` | Service worker that performs the PDF downloads for the sidebar button. |
| `popup.html` / `popup.js` | Toolbar popup: count + export from any tab. |

Browser-only alternatives (no extension install): `crawler/uprera_userscript.user.js`
(Tampermonkey) and `crawler/uprera_collector.js` (console snippet / bookmarklet).

### Workflow

```bash
# 1. Scrape the project list for a district
cd crawler
python scrape_uprera.py --district "Gautam Buddha Nagar" --out-dir ../data

# 2. Collect per-project details — load crawler/uprera_extension/ as an
#    unpacked Chrome extension, then just browse up-rera.in normally.
#    Open "View Detail" and "View Project In Detail" on each project you
#    want captured; export JSON from the sidebar -> uprera_details.json.

# 3. Download the document PDFs in bulk from an export
python download_files.py --details ~/Downloads/uprera_details.json --out ../data/files

# 4. Import into Mongo (from the repo root, real_estate_scripts/)
npm run import:rera -- --details ~/Downloads/uprera_details.json --files ~/Downloads/UPRERA_files
```

Only Gautam Buddha Nagar has been imported into Mongo so far, out of the
districts the list scraper can reach statewide.

---

## Importing curated content

```bash
npm run import:content                     # every JSON in data/content/
npm run import:content -- UPRERAPRJ125561  # one project
npm run validate -- --checklist            # what to extract, by section
```

See the repo-root `CLAUDE.md` for the full publish workflow and the two-layer
data model (`projects` vs `projectcontents`) this all feeds.
