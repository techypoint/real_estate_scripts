// ==UserScript==
// @name         UP-RERA Detail Auto-Collector
// @namespace    uprera.local
// @version      1.0
// @description  Auto-captures every UP-RERA project detail page you open into local storage; one-click export to JSON/CSV. Data renders only in a real browser session, so this rides along with your normal browsing.
// @match        https://www.up-rera.in/View_Registration_Details.aspx*
// @match        https://www.up-rera.in/Projectsummary*
// @match        https://www.up-rera.in/projectsummary*
// @run-at       document-idle
// @grant        none
// ==/UserScript==
/*
 * INSTALL
 *   1. Install the Tampermonkey browser extension (chrome/edge/firefox).
 *   2. Tampermonkey dashboard -> Utilities / "+" -> paste this file -> Save.
 *   3. Browse UP-RERA and open project detail pages the normal way. Each one
 *      you view is captured automatically (a small toast confirms). A floating
 *      panel (bottom-right) shows the running count.
 *   4. Click "Export" in that panel to download uprera_details.json + .csv.
 *      Send me the JSON; merge_details.py joins it to the project list.
 *
 * Same field structure as parse_detail.py and uprera_collector.js.
 * Nothing is sent anywhere; data lives in this browser's localStorage until you
 * export or clear it.
 */
(function () {
  "use strict";
  var STORE_KEY = "uprera_details_v1";

  var FIELD_MAP = {
    "_lblprojectname": "project_name", "_lblregno": "registration_no",
    "_prjregdate": "registration_date", "_prj_type": "project_type",
    "_contactnumber": "coordinator_number", "_Lblpropeseperoid": "proposed_period_months",
    "_lblpropesedDatestart": "proposed_start_date", "_Lblproposedenddt": "declared_completion_date",
    "_promoterMobileNumber": "promoter_mobile_contactbox", "_projectCordinatorNumber": "coordinator_number_contactbox",
    "_helpLineNumber": "helpline_number", "_lblState": "state", "_lbldistrict": "district",
    "_lbltechsil": "tehsil", "_lblpaddress": "project_address", "_Lblpromotername": "promoter_name",
    "_LblAppicanttype": "promoter_applicant_type", "_Lblmobilenumber": "promoter_mobile",
    "_Lblemailapp": "promoter_email", "_Lbladdressapplicant": "promoter_address",
    "_lblpromoterChairmanaddress": "promoter_chairman_address", "_lbltotalproject": "promoter_total_projects",
    "_lbltotalcomplaint": "promoter_total_complaints", "_lblprojectwisecomplaint": "project_complaints"
  };

  function txt(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }
  function endsWith(s) { return document.querySelector('[id$="' + s + '"]'); }
  function loadStore() { try { return JSON.parse(localStorage.getItem(STORE_KEY) || "[]"); } catch (e) { return []; } }
  function saveStore(a) { localStorage.setItem(STORE_KEY, JSON.stringify(a)); }

  function coPromoters() {
    var grid = document.querySelector('[id*="grd_multiple"]'); var out = []; if (!grid) return out;
    var ctls = {};
    [].slice.call(grid.querySelectorAll('[id*="grd_multiple_ctl"]')).forEach(function (el) {
      var m = /grd_multiple_(ctl\d+)_/.exec(el.id); if (m) ctls[m[1]] = 1;
    });
    Object.keys(ctls).sort().forEach(function (c) {
      function gc(s) { return txt(grid.querySelector('[id$="grd_multiple_' + c + s + '"]')); }
      var e = { name: gc("_lblname"), applicant_type: gc("_lbl_apptype"), mobile: gc("_lblmobile"),
                email: gc("_lbl_email"), address: gc("_lbl_prm_address") };
      if (e.name || e.mobile || e.email || e.address) out.push(e);
    });
    return out;
  }
  function documents() {
    var grid = document.querySelector('[id*="grvdocumentdetails"]'); var out = []; if (!grid) return out;
    var ctls = {};
    [].slice.call(grid.querySelectorAll('[id*="grvdocumentdetails_ctl"]')).forEach(function (el) {
      var m = /grvdocumentdetails_(ctl\d+)_/.exec(el.id); if (m) ctls[m[1]] = 1;
    });
    Object.keys(ctls).sort(function (a, b) { return (+a.replace("ctl", "")) - (+b.replace("ctl", "")); }).forEach(function (c) {
      function gd(s) { var e = grid.querySelector('[id$="grvdocumentdetails_' + c + s + '"]'); if (!e) return ""; return e.tagName === "INPUT" ? (e.value || "") : txt(e); }
      var name = gd("_Label2"); if (name) out.push({ sno: gd("_lblSRNO"), name: name, upload_type: gd("_Labdoc"), doc_id: gd("_mainid") });
    });
    return out;
  }
  function extract() {
    var rec = { source_url: location.href, captured_at: new Date().toISOString() };
    Object.keys(FIELD_MAP).forEach(function (s) { rec[FIELD_MAP[s]] = txt(endsWith(s)); });
    var form_id = "", a = document.querySelector('a[href*="mapview_project.aspx?form="]');
    if (a) { var m = /form=(\d+)/.exec(a.getAttribute("href") || ""); if (m) form_id = m[1]; }
    if (!form_id) { var img = endsWith("_imgQRCode"); if (img) { var m2 = /_(\d+)\.png/.exec(img.getAttribute("src") || ""); if (m2) form_id = m2[1]; } }
    rec.form_id = form_id;
    rec.co_promoters = coPromoters();
    rec.documents = documents();
    return rec;
  }

  function capture() {
    var rec = extract();
    if (!rec.registration_no && !rec.project_name) return null; // blank shell
    var store = loadStore();
    var key = rec.registration_no || rec.source_url, idx = -1;
    for (var i = 0; i < store.length; i++) { if ((store[i].registration_no || store[i].source_url) === key) { idx = i; break; } }
    if (idx >= 0) store[idx] = rec; else store.push(rec);
    saveStore(store);
    return rec;
  }

  // ---- UI ----
  function download(name, text, mime) {
    var b = new Blob([text], { type: mime }), a = document.createElement("a");
    a.href = URL.createObjectURL(b); a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(a.href); }, 2000);
  }
  function toCSV(store) {
    var cols = ["registration_no", "project_name", "form_id", "registration_date", "project_type",
      "proposed_period_months", "proposed_start_date", "declared_completion_date", "state", "district",
      "tehsil", "project_address", "coordinator_number", "helpline_number", "promoter_name",
      "promoter_applicant_type", "promoter_mobile", "promoter_email", "promoter_address",
      "promoter_chairman_address", "promoter_total_projects", "promoter_total_complaints",
      "project_complaints", "co_promoter_count", "document_count", "source_url", "captured_at"];
    function esc(v) { v = (v == null ? "" : String(v)); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }
    var lines = [cols.join(",")];
    store.forEach(function (r) {
      lines.push(cols.map(function (c) {
        if (c === "co_promoter_count") return (r.co_promoters || []).length;
        if (c === "document_count") return (r.documents || []).length;
        return esc(r[c]);
      }).join(","));
    });
    return "﻿" + lines.join("\n");
  }

  function toast(msg) {
    var t = document.createElement("div");
    t.textContent = msg;
    t.style.cssText = "position:fixed;bottom:80px;right:16px;z-index:99999;background:#166534;color:#fff;" +
      "padding:8px 14px;border-radius:6px;font:13px/1.3 sans-serif;box-shadow:0 2px 8px rgba(0,0,0,.3)";
    document.body.appendChild(t);
    setTimeout(function () { t.style.transition = "opacity .5s"; t.style.opacity = "0"; }, 1800);
    setTimeout(function () { t.remove(); }, 2400);
  }

  function panel() {
    var p = document.createElement("div");
    p.style.cssText = "position:fixed;bottom:16px;right:16px;z-index:99999;background:#111827;color:#fff;" +
      "padding:10px 12px;border-radius:8px;font:13px/1.4 sans-serif;box-shadow:0 2px 10px rgba(0,0,0,.4)";
    function render() {
      var n = loadStore().length;
      p.innerHTML = "<b>UP-RERA collected: " + n + "</b><br>";
      var exp = document.createElement("button"), clr = document.createElement("button");
      exp.textContent = "Export"; clr.textContent = "Clear";
      [exp, clr].forEach(function (b) { b.style.cssText = "margin-top:6px;margin-right:6px;padding:3px 10px;border:0;border-radius:4px;cursor:pointer"; });
      exp.style.background = "#2563eb"; exp.style.color = "#fff";
      clr.style.background = "#6b7280"; clr.style.color = "#fff";
      exp.onclick = function () { var s = loadStore(); if (!s.length) return; download("uprera_details.json", JSON.stringify(s, null, 2), "application/json"); download("uprera_details.csv", toCSV(s), "text/csv"); };
      clr.onclick = function () { if (confirm("Delete all " + loadStore().length + " records?")) { localStorage.removeItem(STORE_KEY); render(); } };
      p.appendChild(exp); p.appendChild(clr);
    }
    render();
    document.body.appendChild(p);
    return { refresh: render };
  }

  var ui = panel();
  var rec = capture();
  if (rec) { toast("Saved: " + (rec.registration_no || "?") + " — " + (rec.project_name || "")); ui.refresh(); }
})();
