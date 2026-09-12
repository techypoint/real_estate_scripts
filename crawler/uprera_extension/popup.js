/* Popup: shows the collected count and exports/clears via chrome.storage +
 * chrome.downloads. Works from any tab (does not need a UP-RERA tab open). */
(function () {
  "use strict";
  var API = window.UPRERA;

  function refresh() {
    API.getRecords(function (recs) { document.getElementById("count").textContent = recs.length; });
  }

  function downloadText(filename, text, mime) {
    var url = URL.createObjectURL(new Blob([text], { type: mime }));
    chrome.downloads.download({ url: url, filename: filename, saveAs: false }, function () {
      setTimeout(function () { URL.revokeObjectURL(url); }, 4000);
    });
  }

  document.getElementById("expJson").onclick = function () {
    API.getRecords(function (recs) {
      if (!recs.length) return;
      downloadText("uprera_details.json", JSON.stringify(recs, null, 2), "application/json");
    });
  };
  document.getElementById("expCsv").onclick = function () {
    API.getRecords(function (recs) {
      if (!recs.length) return;
      downloadText("uprera_details.csv", API.toCSV(recs), "text/csv");
    });
  };
  document.getElementById("clear").onclick = function () {
    API.getRecords(function (recs) {
      if (recs.length && confirm("Delete all " + recs.length + " collected records?")) { API.setRecords([], refresh); }
    });
  };

  refresh();
})();
