/* Background service worker: performs the actual file downloads.
 * Content scripts can't call chrome.downloads, so the sidebar's "Download files"
 * button messages here with the list of {url, reg, name}, and we save each into
 *   Downloads/UPRERA_files/<registration_no>/<file_name>
 */
function safe(s) {
  return (String(s || "").replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "")) || "file";
}

chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (!msg || msg.type !== "downloadFiles") return;
  var files = msg.files || [];
  var started = 0;
  files.forEach(function (f) {
    if (!f || !f.url) return;
    var path = "UPRERA_files/" + safe(f.reg) + "/" + safe(f.name);
    try {
      chrome.downloads.download({ url: f.url, filename: path, conflictAction: "uniquify" });
      started++;
    } catch (e) { /* ignore individual failures */ }
  });
  sendResponse({ started: started });
  return true; // keep the message channel open for the async response
});
