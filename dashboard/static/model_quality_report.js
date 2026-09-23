(function () {
  "use strict";
  var root = document.getElementById("model-quality-report");
  if (!root) return;
  var hours = document.getElementById("model-quality-hours");
  var status = document.getElementById("model-quality-status");
  var body = document.getElementById("model-quality-body");
  function cell(row, value) {
    var td = document.createElement("td");
    td.textContent = value == null ? "—" : String(value);
    row.appendChild(td);
  }
  function refresh() {
    status.textContent = "Đang tải báo cáo…";
    fetch("/api/ai-learning/model-quality-report?hours=" + encodeURIComponent(hours.value),
      {credentials: "same-origin"})
      .then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); })
      .then(function (report) {
        body.replaceChildren();
        (report.models || []).forEach(function (model) {
          var row = document.createElement("tr");
          cell(row, model.scope_type + " · " + model.scope_key + " · " + (model.metric || "—") +
            " · h" + (model.horizon_hours == null ? "?" : model.horizon_hours));
          cell(row, model.version + " · " + model.status + " · " + model.feature_schema);
          cell(row, model.evaluation_count + " · " + model.quality);
          cell(row, (model.active_mae == null ? "—" : model.active_mae) + " / " +
            (model.candidate_mae == null ? "—" : model.candidate_mae));
          cell(row, (model.active_smape == null ? "—" : model.active_smape) + " / " +
            (model.candidate_smape == null ? "—" : model.candidate_smape));
          cell(row, model.latest_evaluated_at);
          body.appendChild(row);
        });
        if (!body.children.length) {
          var empty = document.createElement("tr");
          var message = document.createElement("td");
          message.colSpan = 6;
          message.textContent = "Chưa có model với cluster_id rõ ràng trong cửa sổ này.";
          empty.appendChild(message);
          body.appendChild(empty);
        }
        status.textContent = report.model_count + " model · " + report.evaluation_count +
          " evaluation trong " + report.window_hours + " giờ" +
          (report.truncated_models || report.truncated_evaluations ? " · báo cáo bị giới hạn" : "");
      })
      .catch(function () { status.textContent = "Không tải được báo cáo chất lượng model."; });
  }
  hours.addEventListener("change", refresh);
  refresh();
}());
