/* Shared helpers for the UP-RERA Detail Collector extension.
 * Loaded by both content.js (as a plain global) and popup.js (via <script>).
 * Field structure matches parse_detail.py exactly.
 */
(function (root) {
  "use strict";

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

  var CSV_COLS = ["registration_no", "project_name", "form_id", "registration_date", "project_type",
    "proposed_period_months", "proposed_start_date", "declared_completion_date", "state", "district",
    "tehsil", "project_address", "coordinator_number", "helpline_number", "promoter_name",
    "promoter_applicant_type", "promoter_mobile", "promoter_email", "promoter_address",
    "promoter_chairman_address", "promoter_total_projects", "promoter_total_complaints",
    "project_complaints", "co_promoter_count", "document_count", "source_url", "captured_at"];

  function toCSV(store) {
    function esc(v) { v = (v == null ? "" : String(v)); return /[",\n]/.test(v) ? '"' + v.replace(/"/g, '""') + '"' : v; }
    var lines = [CSV_COLS.join(",")];
    store.forEach(function (r) {
      lines.push(CSV_COLS.map(function (c) {
        if (c === "co_promoter_count") return (r.co_promoters || []).length;
        if (c === "document_count") return (r.documents || []).length;
        return esc(r[c]);
      }).join(","));
    });
    return "﻿" + lines.join("\n");
  }

  // chrome.storage.local wrappers (records array under key "records")
  function getRecords(cb) { chrome.storage.local.get({ records: [] }, function (o) { cb(o.records || []); }); }
  function setRecords(recs, cb) { chrome.storage.local.set({ records: recs }, cb || function () {}); }

  root.UPRERA = { FIELD_MAP: FIELD_MAP, CSV_COLS: CSV_COLS, toCSV: toCSV, getRecords: getRecords, setRecords: setRecords };
})(typeof window !== "undefined" ? window : this);
