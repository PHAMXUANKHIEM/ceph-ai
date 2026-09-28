// Autonomy KPIs (autonomy plan WP0): read-only, admin-only; hidden on error.
(function () {
  var grid = document.getElementById("autonomy-kpi");
  if (!grid) return;
  var set = function (key, value) {
    var node = grid.querySelector('[data-kpi="' + key + '"]');
    if (node) node.textContent = value;
  };
  var percent = function (value) {
    return value === null || value === undefined ? "—" : (value * 100).toFixed(1) + "%";
  };
  fetch(grid.dataset.endpoint, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then(function (response) { if (!response.ok) throw new Error(String(response.status)); return response.json(); })
    .then(function (report) {
      set("per_week", report.incidents.per_week === null ? "—" : String(report.incidents.per_week));
      set("reopen", "mở lại < 30 phút: " + percent(report.incidents.reopen_rate));
      set("placeholder", percent(report.actions.investigate_manually_rate));
      set("verdicts", report.verdicts.labelled + " / " + report.verdicts.cases);
      set("verified", report.verdicts.verified_success + " case VERIFIED_SUCCESS");
      grid.hidden = false;
    })
    .catch(function () { grid.hidden = true; });
})();
