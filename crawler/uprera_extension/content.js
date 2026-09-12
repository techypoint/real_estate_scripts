/* UP-RERA Detail Collector -- content script.
 * Captures two kinds of pages into chrome.storage.local, merged per project
 * (keyed by registration number):
 *   - Projectsummary / View_Registration_Details  -> top-level summary fields
 *   - viewprojects ("View Project In Detail")      -> rec.detail (rich page:
 *     agents, permits, dev works, bank accounts, land, plan rows, khasra,
 *     registry, and all documents WITH direct Azure blob download URLs)
 * A floating tab on the right edge opens a sidebar (isolated Shadow DOM) with
 * count, per-page capture, export and clear.
 */
(function () {
  "use strict";
  var API = window.UPRERA;
  var BLOB_BASE = "https://upreradisk.blob.core.windows.net/azureportaldeploy/";

  function txt(el) { return el ? el.textContent.replace(/\s+/g, " ").trim() : ""; }
  function endsWith(s) { return document.querySelector('[id$="' + s + '"]'); }
  function clean(t) { return t.replace(/^[^:]{2,40}:\s*/, "").trim(); }

  /* ---- generic ASP.NET GridView -> array of {header: cell} ---- */
  function rowsOf(table) {
    if (!table) return [];
    var headers = [].slice.call(table.querySelectorAll("th")).map(txt);
    var out = [];
    [].slice.call(table.querySelectorAll("tr")).forEach(function (tr) {
      if (tr.querySelector("th")) return;
      var tds = [].slice.call(tr.querySelectorAll("td")); if (!tds.length) return;
      var vals = tds.map(txt); if (!vals.some(Boolean)) return;
      var o = {};
      if (headers.length === vals.length) vals.forEach(function (v, i) { o[headers[i] || ("col" + i)] = v; });
      else vals.forEach(function (v, i) { o["col" + i] = v; });
      out.push(o);
    });
    return out;
  }
  function grid(sub, excl) {
    var els = [].slice.call(document.querySelectorAll('table[id*="' + sub + '"]'));
    if (excl) els = els.filter(function (t) { return t.id.indexOf(excl) < 0; });
    return els[0] || null;
  }

  /* ============================ SUMMARY page ============================ */
  function extractSummary() {
    var rec = { source_url: location.href, captured_at: new Date().toISOString() };
    Object.keys(API.FIELD_MAP).forEach(function (s) { rec[API.FIELD_MAP[s]] = txt(endsWith(s)); });
    var form_id = "", a = document.querySelector('a[href*="mapview_project.aspx?form="]');
    if (a) { var m = /form=(\d+)/.exec(a.getAttribute("href") || ""); if (m) form_id = m[1]; }
    if (!form_id) { var img = endsWith("_imgQRCode"); if (img) { var m2 = /_(\d+)\.png/.exec(img.getAttribute("src") || ""); if (m2) form_id = m2[1]; } }
    rec.form_id = form_id;
    // co-promoters
    var co = [], gm = grid("grd_multiple");
    if (gm) {
      var ct = {};
      [].slice.call(gm.querySelectorAll('[id*="grd_multiple_ctl"]')).forEach(function (el) { var m = /grd_multiple_(ctl\d+)_/.exec(el.id); if (m) ct[m[1]] = 1; });
      Object.keys(ct).sort().forEach(function (c) {
        function gc(s) { return txt(gm.querySelector('[id$="grd_multiple_' + c + s + '"]')); }
        var e = { name: gc("_lblname"), applicant_type: gc("_lbl_apptype"), mobile: gc("_lblmobile"), email: gc("_lbl_email"), address: gc("_lbl_prm_address") };
        if (e.name || e.mobile || e.email || e.address) co.push(e);
      });
    }
    rec.co_promoters = co;
    return rec;
  }

  /* ========================== VIEWPROJECTS page ========================= */
  var VP_SCALARS = {
    "_lblProjectNameHeading": "project_name", "_lblProjectNameWithID": "project_id_raw",
    "_lblregisdate": "registration_date", "_lblPromoterNameHeading": "promoter_name",
    "_lblPromoterNameWithID": "promoter_id_raw", "_lblProjectType": "project_type",
    "_lblProjectCategory": "project_category", "_lblRegistrationFee": "registration_fee",
    "_lblState": "state", "_lblProjectDuration": "project_duration_months", "_ddlTehsil_old": "tehsil"
  };
  var VP_GRIDS = [
    ["grvagents", "agents", null], ["grdPermitDetails", "permits", null],
    ["grddevlopmentworks", "development_works", null], ["grdCollection", "account_collection", null],
    ["grdSeparate", "account_separate", null], ["grdTransaction", "account_transaction", null],
    ["grdLadDetail_doc", "land_documents", null], ["grdLadDetail", "land_details", "_doc"],
    ["grd_PlanDetails_ForAdmin", "plan_details", null], ["grdKhasra", "khasra", null],
    ["grdRegistryAgreementDetails", "registry_agreements", null]
  ];

  function extractViewProject() {
    var d = { source_url: location.href, captured_at: new Date().toISOString() };
    Object.keys(VP_SCALARS).forEach(function (s) { var el = endsWith(s); d[VP_SCALARS[s]] = el ? clean(txt(el)) : ""; });
    var reg = (/(UPRERAPRJ\d+)/.exec(d.project_id_raw || "") || [])[1] || "";
    var prm = (/(UPRERAPRM\d+)/.exec(d.promoter_id_raw || "") || [])[1] || "";
    d.registration_no = reg; d.promoter_id = prm;
    VP_GRIDS.forEach(function (g) { d[g[1]] = rowsOf(grid(g[0], g[2])); });

    // documents with direct blob URLs
    var docs = [], dt = grid("grvdocumentdetails");
    if (dt) {
      var headers = [].slice.call(dt.querySelectorAll("th")).map(txt);
      function idx(pred, dflt) { for (var i = 0; i < headers.length; i++) if (pred(headers[i].toLowerCase())) return i; return dflt; }
      var fi = idx(function (h) { return h.indexOf("file name") >= 0; }, 2);
      var ni = idx(function (h) { return h === "document name"; }, 1);
      var di = idx(function (h) { return h.indexOf("date") >= 0; }, -1);
      var ti = idx(function (h) { return h.indexOf("doc type") >= 0 || h.indexOf("upload doc") >= 0; }, -1);
      [].slice.call(dt.querySelectorAll("tr")).forEach(function (tr) {
        if (tr.querySelector("th")) return;
        var tds = [].slice.call(tr.querySelectorAll("td")); if (tds.length <= fi) return;
        var fname = txt(tds[fi]); if (!fname) return;
        docs.push({
          document_name: ni >= 0 && tds[ni] ? txt(tds[ni]) : "",
          file_name: fname,
          uploaded_date: di >= 0 && tds[di] ? txt(tds[di]) : "",
          upload_type: ti >= 0 && tds[ti] ? txt(tds[ti]) : "",
          blob_url: BLOB_BASE + encodeURIComponent(fname)
        });
      });
    }
    d.documents = docs;
    return d;
  }

  function isViewProject() { return !!(endsWith("_lblProjectNameHeading") || grid("grd_PlanDetails_ForAdmin")); }
  function isSummary() { return !!(endsWith("_lblregno") || endsWith("_lblprojectname")); }

  /* ------------------------------ storage merge ------------------------- */
  function saveInto(reg, name, form_id, apply, ui, label) {
    if (!reg && !name) { if (ui.manual) ui.toast("No project data on this page", false); return; }
    API.getRecords(function (store) {
      var key = reg || name, idx = -1;
      for (var i = 0; i < store.length; i++) { if ((store[i].registration_no || "") === reg && reg) { idx = i; break; } }
      var rec = idx >= 0 ? store[idx] : { registration_no: reg, captured_at: new Date().toISOString() };
      if (name && !rec.project_name) rec.project_name = name;
      if (form_id) rec.form_id = form_id;
      apply(rec);
      rec.captured_at = new Date().toISOString();
      if (idx >= 0) store[idx] = rec; else store.push(rec);
      API.setRecords(store, function () { ui.refresh(); ui.toast(label + ": " + (reg || "?") + " — " + (rec.project_name || ""), true); });
    });
  }

  function captureSummary(ui) {
    var s = extractSummary();
    saveInto(s.registration_no, s.project_name, s.form_id, function (rec) {
      // copy the flat summary fields onto the record
      Object.keys(API.FIELD_MAP).forEach(function (k) { rec[API.FIELD_MAP[k]] = s[API.FIELD_MAP[k]]; });
      rec.co_promoters = s.co_promoters;
      rec.source_url = s.source_url;
    }, ui, "Summary saved");
  }

  function captureViewProject(ui) {
    var d = extractViewProject();
    saveInto(d.registration_no, d.project_name, d.form_id, function (rec) {
      rec.detail = d;
    }, ui, "Full detail saved (" + d.documents.length + " files)");
  }

  function capture(ui) {
    if (isViewProject()) captureViewProject(ui);
    else if (isSummary()) captureSummary(ui);
    else if (ui.manual) ui.toast("No project data on this page", false);
  }

  function download(name, text, mime) {
    var url = URL.createObjectURL(new Blob([text], { type: mime })), a = document.createElement("a");
    a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 2000);
  }

  /* --------------------------------- UI --------------------------------- */
  var CSS = "" +
    ":host{all:initial}*{box-sizing:border-box;font-family:'Segoe UI',system-ui,sans-serif}" +
    ".tab{position:fixed;top:38%;right:0;z-index:2147483647;background:#b91c1c;color:#fff;cursor:pointer;" +
      "writing-mode:vertical-rl;transform:rotate(180deg);padding:14px 7px;border-radius:8px 0 0 8px;font-size:13px;" +
      "font-weight:700;letter-spacing:.5px;box-shadow:-2px 2px 8px rgba(0,0,0,.3);user-select:none}" +
    ".tab .n{background:#fff;color:#b91c1c;border-radius:10px;padding:1px 6px;margin-top:6px;transform:rotate(180deg);" +
      "writing-mode:horizontal-tb;font-size:11px;display:inline-block}" +
    ".bar{position:fixed;top:0;right:0;height:100%;width:330px;max-width:90vw;z-index:2147483647;background:#0f172a;" +
      "color:#e5e7eb;box-shadow:-4px 0 18px rgba(0,0,0,.45);transform:translateX(100%);transition:transform .22s ease;display:flex;flex-direction:column}" +
    ".bar.open{transform:translateX(0)}" +
    ".hd{display:flex;align-items:center;justify-content:space-between;padding:14px 16px;background:#b91c1c}" +
    ".hd h1{margin:0;font-size:15px;color:#fff}.x{cursor:pointer;color:#fff;font-size:20px;border:0;background:none}" +
    ".body{padding:16px;overflow-y:auto;flex:1}" +
    ".count{font-size:30px;font-weight:800;color:#fff}.count small{font-size:13px;font-weight:400;color:#94a3b8;margin-left:6px}" +
    "button.act{display:block;width:100%;margin:8px 0;padding:9px;border:0;border-radius:6px;cursor:pointer;color:#fff;font-size:13px;font-weight:600}" +
    "#cap{background:#0891b2}#ej{background:#2563eb}#ec{background:#059669}#df{background:#7c3aed}#cl{background:#475569}" +
    ".sec{margin-top:14px;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:#94a3b8}" +
    ".list{margin-top:6px;border-top:1px solid #1e293b}" +
    ".item{padding:7px 2px;border-bottom:1px solid #1e293b;font-size:12px}.item b{color:#fff}.item span{color:#94a3b8;display:block}" +
    ".badge{display:inline-block;font-size:10px;padding:1px 6px;border-radius:8px;margin-left:6px}" +
    ".yes{background:#166534;color:#fff}.no{background:#374151;color:#9ca3af}" +
    ".hint{color:#64748b;font-size:11px;margin-top:14px;line-height:1.5}" +
    ".toast{position:fixed;bottom:20px;right:350px;z-index:2147483647;color:#fff;padding:9px 14px;border-radius:6px;font-size:13px;box-shadow:0 2px 8px rgba(0,0,0,.35);transition:opacity .5s}";

  function buildUI() {
    var host = document.createElement("div"); host.id = "uprera-collector-host";
    (document.body || document.documentElement).appendChild(host);
    var root = host.attachShadow({ mode: "open" });
    var style = document.createElement("style"); style.textContent = CSS; root.appendChild(style);

    var tab = document.createElement("div"); tab.className = "tab";
    tab.innerHTML = "UP-RERA COLLECTOR<span class='n' id='tabn'>0</span>"; root.appendChild(tab);

    var bar = document.createElement("div"); bar.className = "bar";
    bar.innerHTML =
      "<div class='hd'><h1>UP-RERA Collector</h1><button class='x' id='x'>&times;</button></div>" +
      "<div class='body'>" +
        "<div class='count'><span id='cnt'>0</span><small>projects saved</small></div>" +
        "<button class='act' id='cap'>Capture this page</button>" +
        "<button class='act' id='df'>Download files (PDFs)</button>" +
        "<button class='act' id='ej'>Export JSON</button>" +
        "<button class='act' id='ec'>Export CSV</button>" +
        "<button class='act' id='cl'>Clear all</button>" +
        "<div class='sec'>Recently saved</div><div class='list' id='list'></div>" +
        "<div class='hint'>Open a project's <b>View Detail</b> page for the summary, then " +
        "<b>View Project In Detail</b> for the full page + document files. Both merge into one " +
        "record per project. Export here and send me the JSON.</div>" +
      "</div>";
    root.appendChild(bar);

    var $ = function (id) { return root.getElementById(id); };
    function open() { bar.classList.add("open"); } function close() { bar.classList.remove("open"); }
    tab.onclick = open; $("x").onclick = close;

    function refresh() {
      API.getRecords(function (recs) {
        $("cnt").textContent = recs.length; $("tabn").textContent = recs.length;
        var html = "";
        recs.slice().reverse().slice(0, 40).forEach(function (r) {
          var sum = r.registration_no && (r.project_name || r.promoter_name);
          var det = r.detail ? (r.detail.documents || []).length : 0;
          html += "<div class='item'><b>" + (r.registration_no || "?") + "</b>" +
                  "<span>" + (r.project_name || "") +
                  "<span class='badge " + (sum ? "yes" : "no") + "'>summary</span>" +
                  "<span class='badge " + (r.detail ? "yes" : "no") + "'>detail" + (r.detail ? " · " + det + " files" : "") + "</span>" +
                  "</span></div>";
        });
        $("list").innerHTML = html || "<div class='item'><span>Nothing yet.</span></div>";
      });
    }
    function toast(msg, ok) {
      var t = document.createElement("div"); t.className = "toast"; t.textContent = msg;
      t.style.background = ok ? "#166534" : "#92400e"; root.appendChild(t);
      setTimeout(function () { t.style.opacity = "0"; }, 2000); setTimeout(function () { t.remove(); }, 2600);
    }

    var ui = { refresh: refresh, toast: toast, open: open, close: close, manual: false };
    $("cap").onclick = function () { ui.manual = true; capture(ui); ui.manual = false; };
    $("df").onclick = function () {
      API.getRecords(function (recs) {
        var files = [], projects = 0;
        recs.forEach(function (r) {
          var docs = (r.detail && r.detail.documents) || [];
          if (docs.length) projects++;
          docs.forEach(function (d) { if (d.blob_url && d.file_name) files.push({ url: d.blob_url, reg: r.registration_no || "unknown", name: d.file_name }); });
        });
        if (!files.length) { toast("No files captured yet — open a 'View Project In Detail' page first", false); return; }
        if (!confirm("Download " + files.length + " PDF files from " + projects + " project(s)?\nThey save to Downloads/UPRERA_files/<reg no>/.")) return;
        chrome.runtime.sendMessage({ type: "downloadFiles", files: files }, function () {
          toast("Downloading " + files.length + " files… (allow multiple downloads if prompted)", true);
        });
      });
    };
    $("ej").onclick = function () { API.getRecords(function (r) { if (r.length) download("uprera_details.json", JSON.stringify(r, null, 2), "application/json"); else toast("Nothing to export", false); }); };
    $("ec").onclick = function () { API.getRecords(function (r) { if (r.length) download("uprera_details.csv", API.toCSV(r), "text/csv"); else toast("Nothing to export", false); }); };
    $("cl").onclick = function () { API.getRecords(function (r) { if (r.length && confirm("Delete all " + r.length + " records?")) API.setRecords([], refresh); }); };
    refresh();
    return ui;
  }

  var ui = buildUI();
  capture(ui);   // auto-capture on load
})();
