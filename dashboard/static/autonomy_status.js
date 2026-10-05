// Self-learning progress (autonomy plan WP8): read-only, admin-only; hidden on error.
(function () {
  var grid = document.getElementById("autonomy-status");
  if (!grid) return;
  var set = function (key, value) {
    var node = grid.querySelector('[data-status="' + key + '"]');
    if (node) node.textContent = value;
  };
  var percent = function (value) {
    return value === null || value === undefined ? "—" : (value * 100).toFixed(1) + "%";
  };
  var sum = function (counts) {
    return Object.keys(counts || {}).reduce(function (total, key) { return total + counts[key]; }, 0);
  };
  fetch(grid.dataset.endpoint, { credentials: "same-origin", headers: { Accept: "application/json" } })
    .then(function (response) { if (!response.ok) throw new Error(String(response.status)); return response.json(); })
    .then(function (report) {
      var learning = report.online_learning;
      if (learning) {
        set("ol-scored", learning.scored + " mẫu đã học");
        set("ol-detail", learning.verified + " kết quả đã xác minh · " + learning.mode + " · " + (learning.decision || "—"));
      } else {
        set("ol-detail", "không đọc được dữ liệu");
      }
      var evidence = report.evidence;
      if (evidence) {
        set("ev-investigated", evidence.incidents_investigated + " incident");
        set("ev-detail", evidence.triaged + " triage · luật kết luận " + percent(evidence.rule_coverage));
      }
      var decisions = report.decisions;
      if (decisions) {
        set("dec-total", String(sum(decisions.by_source)));
        set("dec-detail", "shadow: " + (Object.keys(decisions.shadow || {}).map(function (key) {
          return key + " " + decisions.shadow[key];
        }).join(" · ") || "chưa có"));
      }
      set("frr", report.false_release_rate === null ? "chưa đủ nhãn" : percent(report.false_release_rate));
      var ope = report.ope;
      set("ope", ope ? "OPE " + ope.window_days + " ngày (toàn hệ thống): " + ope.decisions_with_reward + " quyết định có reward" : "OPE: không có dữ liệu");
      var errors = Object.keys(report.errors || {});
      var note = document.getElementById("autonomy-status-errors");
      if (note && errors.length) {
        note.textContent = "Thiếu mục: " + errors.join(", ");
        note.hidden = false;
      }
      grid.hidden = false;
    })
    .catch(function () { grid.hidden = true; });
})();
