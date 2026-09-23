(function () {
  "use strict";
  var root = document.getElementById("rbd-logical-chart");
  if (!root) return;
  var select = document.getElementById("rbd-logical-series");
  var status = document.getElementById("rbd-logical-status");
  var svg = document.getElementById("rbd-logical-svg");
  var data = [];
  var ns = "http://www.w3.org/2000/svg";

  function element(name, attributes) {
    var node = document.createElementNS(ns, name);
    Object.keys(attributes).forEach(function (key) { node.setAttribute(key, String(attributes[key])); });
    svg.appendChild(node);
  }

  function draw(series) {
    while (svg.firstChild) svg.removeChild(svg.firstChild);
    if (!series || series.status !== "ADVISORY" || !series.history || series.history.length < 2) {
      svg.hidden = true;
      status.textContent = series ? "Chưa đủ lịch sử mới cho chuỗi này: " + series.reason : "Chưa có dữ liệu RBD logic.";
      return;
    }
    var values = series.history.map(function (point) { return Number(point.bytes); });
    var upper = Math.max.apply(null, values.concat([Number(series.prediction_high_bytes)]));
    var lower = Math.min.apply(null, values.concat([Number(series.prediction_low_bytes)]));
    var span = Math.max(1, upper - lower);
    function y(value) { return 190 - (Number(value) - lower) / span * 150; }
    var step = 500 / Math.max(1, values.length - 1);
    var points = values.map(function (value, index) { return (45 + index * step).toFixed(1) + "," + y(value).toFixed(1); }).join(" ");
    element("line", {x1: 45, y1: 190, x2: 595, y2: 190, stroke: "#475569"});
    element("polyline", {points: points, fill: "none", stroke: "#22d3ee", "stroke-width": 3});
    element("rect", {x: 574, y: y(series.prediction_high_bytes), width: 18,
      height: Math.max(2, y(series.prediction_low_bytes) - y(series.prediction_high_bytes)),
      fill: "#22d3ee", "fill-opacity": 0.2});
    element("line", {x1: 545, y1: y(values[values.length - 1]), x2: 583,
      y2: y(series.prediction_bytes), stroke: "#22d3ee", "stroke-width": 3, "stroke-dasharray": "5 4"});
    var label = series.metric.replace(/_/g, " ");
    status.textContent = label + " · hiện tại " + (series.current_bytes / 1073741824).toFixed(2) +
      " GiB · dự báo 7 ngày " + (series.prediction_bytes / 1073741824).toFixed(2) +
      " GiB · khoảng " + (series.prediction_low_bytes / 1073741824).toFixed(2) + "–" +
      (series.prediction_high_bytes / 1073741824).toFixed(2) + " GiB · confidence " +
      (series.confidence * 100).toFixed(1) + "% · mẫu cuối " + series.observed_at;
    svg.hidden = false;
  }

  select.addEventListener("change", function () { draw(data[Number(select.value)]); });
  fetch("/api/capacity-forecast/rbd-logical?cluster_id=" + encodeURIComponent(root.dataset.clusterId),
    {credentials: "same-origin"})
    .then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); })
    .then(function (report) {
      (report.pools || []).forEach(function (pool) {
        (pool.series || []).forEach(function (series) {
          var option = document.createElement("option");
          option.value = String(data.length);
          option.textContent = pool.pool + " · " + series.metric;
          select.appendChild(option);
          data.push(series);
        });
      });
      select.disabled = data.length === 0;
      draw(data[0]);
    })
    .catch(function () { select.disabled = true; status.textContent = "Không tải được dự báo RBD logic."; });
}());
