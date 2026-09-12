# UP-RERA Detail Collector — Chrome extension

Auto-captures UP-RERA project **detail** pages as you browse, and exports them
to JSON/CSV. The detail data renders only inside a genuine browser session
(the site blocks automated clients), so this extension simply rides along with
your normal browsing and structures whatever page you open.

## Install (unpacked)

1. Open `chrome://extensions` in Chrome (or Edge: `edge://extensions`).
2. Turn on **Developer mode** (top-right).
3. Click **Load unpacked** and select this `uprera_extension/` folder.
4. (Optional) Pin the extension so its icon is visible.

Works in any Chromium browser (Chrome, Edge, Brave).

## Use

1. Browse up-rera.in and open a project's **View Detail** / Project Summary page
   the normal way. Each page you open is saved automatically — a green toast
   confirms.
2. A red **"UP-RERA COLLECTOR"** tab sits on the **right edge** of every detail
   page, with a live count badge. Click it to slide out the **sidebar**, which has:
   - **projects saved** count and a list of recently saved ones,
   - **Capture this page** (re-grab, if auto-capture missed it),
   - **Download files (PDFs)** — downloads every captured project's documents
     straight to `Downloads/UPRERA_files/<registration_no>/`. Chrome may ask once
     to "allow multiple downloads" — click Allow.
   - **Export JSON**, **Export CSV**,
   - **Clear all**.
   The extension's **toolbar icon** opens the same export options from any tab.
3. Send me the exported JSON — `merge_details.py` joins it to the district
   project list on registration number.

The store persists across page loads and browser restarts (chrome.storage) until
you press **Clear**. Re-opening a project you already captured just updates it —
no duplicates. The sidebar lives in an isolated Shadow DOM, so the site's styles
can't affect it. Nothing is sent anywhere; data stays in your browser until export.

## Fields captured

project name, registration number, `form_id`, registration/start/completion
dates, project type, coordinator number, district/tehsil/address, promoter
(name, type, mobile, email, address, chairman), co-promoters, complaints, and
the full document list. Identical structure to `parse_detail.py`.
