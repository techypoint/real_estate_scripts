/* ============================================================================
 * UP-RERA project-detail collector  (manual-assisted crawling)
 * ----------------------------------------------------------------------------
 * WHY THIS EXISTS
 *   up-rera.in renders a project's full detail page ("Projectsummary") ONLY
 *   inside a genuine, human-driven browser session. Every automated client
 *   (requests / curl / curl_cffi / Playwright, even real Chrome driven by
 *   automation) receives a blank "shell" instead. So the page-load has to be
 *   done by you, a human. Reading + structuring the data is NOT gated -- that's
 *   this script's job. Field extraction here mirrors parse_detail.py exactly.
 *
 * WORKFLOW
 *   1. Open a project's detail page in your normal Chrome and confirm you can
 *      SEE the fields (Project Name, Registration Number, promoter...). If it
 *      looks blank, that load got gated -- reopen it and try again.
 *   2. Run  reraGrab()  (paste this file in the console once, or use the
 *      bookmarklet printed by reraHelp()). It saves one record to localStorage.
 *      Re-grabbing the same registration number just updates it (no dupes).
 *   3. Repeat across as many projects / sittings as you like.
 *   4. Run  reraExport()  to download uprera_details.json (+ .csv). Send me the
 *      JSON; merge_details.py joins it to the district list on registration no.
 *
 * Nothing here sends data anywhere -- it stays in your browser until you export.
 * ==========================================================================*/

(function () {
  "use strict";
  var STORE_KEY = "uprera_details_v1";

  // suffix of element id -> output field name  (identical to parse_detail.py)
  var FIELD_MAP = {
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
    "_lblprojectwisecomplaint": "project_complaints"
  };

  function txt(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }
  function endsWith(suffix, root) { return (root || document).querySelector('[id$="' + suffix + '"]'); }

  function loadStore() { try { return JSON.parse(localStorage.getItem(STORE_KEY) || "[]"); } catch (e) { return []; } }
  function saveStore(a) { localStorage.setItem(STORE_KEY, JSON.stringify(a)); }

  function extractCoPromoters() {
    var grid = document.querySelector('[id*="grd_multiple"]');
    var out = [];
    if (!grid) return out;
    var ctls = {};
    [].slice.call(grid.querySelectorAll('[id*="grd_multiple_ctl"]')).forEach(function (el) {
      var m = /grd_multiple_(ctl\d+)_/.exec(el.id); if (m) ctls[m[1]] = 1;
    });
    Object.keys(ctls).sort().forEach(function (ctl) {
      function gc(s) { var e = grid.querySelector('[id$="grd_multiple_' + ctl + s + '"]'); return txt(e); }
      var e = { name: gc("_lblname"), applicant_type: gc("_lbl_apptype"),
                mobile: gc("_lblmobile"), email: gc("_lbl_email"), address: gc("_lbl_prm_address") };
      if (e.name || e.mobile || e.email || e.address) out.push(e);
    });
    return out;
  }

  function extractDocuments() {
    var grid = document.querySelector('[id*="grvdocumentdetails"]');
    var out = [];
    if (!grid) return out;
    var ctls = {};
    [].slice.call(grid.querySelectorAll('[id*="grvdocumentdetails_ctl"]')).forEach(function (el) {
      var m = /grvdocumentdetails_(ctl\d+)_/.exec(el.id); if (m) ctls[m[1]] = 1;
    });
    Object.keys(ctls).sort(function (a, b) { return (+a.replace("ctl", "")) - (+b.replace("ctl", "")); })
      .forEach(function (ctl) {
        function gd(s) {
          var e = grid.querySelector('[id$="grvdocumentdetails_' + ctl + s + '"]');
          if (!e) return "";
          return e.tagName === "INPUT" ? (e.value || "") : txt(e);
        }
        var name = gd("_Label2");
        if (name) out.push({ sno: gd("_lblSRNO"), name: name, upload_type: gd("_Labdoc"), doc_id: gd("_mainid") });
      });
    return out;
  }

  function extractDetail() {
    var rec = { source_url: location.href, captured_at: new Date().toISOString() };
    Object.keys(FIELD_MAP).forEach(function (suffix) { rec[FIELD_MAP[suffix]] = txt(endsWith(suffix)); });

    // internal numeric project id, from the map link or QR image filename
    var form_id = "";
    var a = document.querySelector('a[href*="mapview_project.aspx?form="]');
    if (a) { var m = /form=(\d+)/.exec(a.getAttribute("href") || ""); if (m) form_id = m[1]; }
    if (!form_id) { var img = endsWith("_imgQRCode"); if (img) { var m2 = /_(\d+)\.png/.exec(img.getAttribute("src") || ""); if (m2) form_id = m2[1]; } }
    rec.form_id = form_id;

    rec.co_promoters = extractCoPromoters();
    rec.documents = extractDocuments();

    // safety net: every identifiable data span, so nothing is ever lost
    var raw = {};
    [].slice.call(document.querySelectorAll('[id*="ContentPlaceHolder1_"]')).forEach(function (s) {
      if (s.tagName === "LABEL" || s.tagName === "SPAN") { var v = txt(s); if (v) raw[s.id.replace(/^ctl00_ContentPlaceHolder1_/, "")] = v; }
    });
    rec.raw_spans = raw;
    return rec;
  }

  window.reraGrab = function () {
    var rec = extractDetail();
    if (!rec.registration_no && !rec.project_name) {
      console.warn("[UP-RERA] This page looks BLANK (bot-gate shell). Nothing saved. Reopen the detail page and try again.");
      return rec;
    }
    var store = loadStore();
    var key = rec.registration_no || rec.source_url;
    var idx = -1;
    for (var i = 0; i < store.length; i++) { if ((store[i].registration_no || store[i].source_url) === key) { idx = i; break; } }
    if (idx >= 0) store[idx] = rec; else store.push(rec);
    saveStore(store);
    console.log("[UP-RERA] saved: " + (rec.registration_no || "?") + " — " + (rec.project_name || "?") +
                "   (total collected: " + store.length + ")");
    return rec;
  };

  window.reraCount = function () { var n = loadStore().length; console.log("[UP-RERA] " + n + " projects collected."); return n; };
  window.reraList = function () { return loadStore().map(function (r) { return r.registration_no + " — " + r.project_name; }); };

  function download(name, text, mime) {
    var blob = new Blob([text], { type: mime });
    var a = document.createElement("a");
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
  }

  function toCSV(store) {
    var cols = ["registration_no", "project_name", "form_id", "registration_date", "project_type",
      "proposed_period_months", "proposed_start_date", "declared_completion_date",
      "state", "district", "tehsil", "project_address", "coordinator_number", "helpline_number",
      "promoter_name", "promoter_applicant_type", "promoter_mobile", "promoter_email",
      "promoter_address", "promoter_chairman_address", "promoter_total_projects",
      "promoter_total_complaints", "project_complaints", "co_promoter_count", "document_count",
      "source_url", "captured_at"];
    function esc(v) { v = (v == null ? "" : String(v)); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }
    var lines = [cols.join(",")];
    store.forEach(function (r) {
      var row = cols.map(function (c) {
        if (c === "co_promoter_count") return (r.co_promoters || []).length;
        if (c === "document_count") return (r.documents || []).length;
        return esc(r[c]);
      });
      lines.push(row.join(","));
    });
    return "﻿" + lines.join("\n");
  }

  window.reraExport = function () {
    var store = loadStore();
    if (!store.length) { console.warn("[UP-RERA] nothing to export yet."); return; }
    download("uprera_details.json", JSON.stringify(store, null, 2), "application/json");
    download("uprera_details.csv", toCSV(store), "text/csv");
    console.log("[UP-RERA] exported " + store.length + " projects (JSON + CSV).");
  };

  window.reraClear = function () {
    if (confirm("Delete ALL " + loadStore().length + " collected UP-RERA records?")) {
      localStorage.removeItem(STORE_KEY); console.log("[UP-RERA] store cleared.");
    }
  };

  window.reraHelp = function () {
    console.log([
      "UP-RERA collector loaded. Commands:",
      "  reraGrab()    - scrape the detail page you're on and save it",
      "  reraCount()   - how many projects collected so far",
      "  reraList()    - list collected reg-no / names",
      "  reraExport()  - download uprera_details.json + .csv",
      "  reraClear()   - wipe the local store",
      "",
      "One-click bookmarklet (create a bookmark whose URL is the line below):",
      "  javascript:(" + window.reraGrab.toString() + ")();void 0;"
    ].join("\n"));
  };

  window.reraHelp();
})();
