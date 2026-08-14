(function () {
  var panel = document.getElementById("vm-perf-panel");
  if (!panel) return;

  var form = document.getElementById("vm-perf-form");
  var errorEl = document.getElementById("vm-perf-error");
  var progressEl = document.getElementById("vm-perf-progress");
  var resultEl = document.getElementById("vm-perf-result");
  var status = panel.dataset.status || null;
  var timer = null;

  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = String(value == null ? "" : value);
    return div.innerHTML;
  }

  function fmt(value, digits) {
    return typeof value === "number" ? value.toFixed(digits == null ? 1 : digits) : "—";
  }

  function setError(message) {
    if (!errorEl) return;
    errorEl.hidden = !message;
    errorEl.textContent = message || "";
  }

  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      setError(null);
      var button = form.querySelector("button[type=submit]");
      button.disabled = true;
      var data = new FormData(form);
      fetch("/volumes/vm-perf/propose", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          vm_ip: data.get("vm_ip"),
          ssh_user: data.get("ssh_user"),
          ssh_key_path: data.get("ssh_key_path"),
          device: data.get("device")
        })
      }).then(function (response) {
        if (!response.ok) {
          return response.json().then(function (body) {
            throw new Error(body.detail || "HTTP " + response.status);
          });
        }
        return response.json();
      }).then(function () {
        window.location.reload();
      }).catch(function (error) {
        button.disabled = false;
        setError(error.message);
      });
    });
  }

  function render(progress) {
    if (!progress || !progress.length) return;
    progressEl.hidden = false;
    progressEl.innerHTML = progress.map(function (step) {
      var glyph = step.status === "done" ? "✅" : step.status === "failed" ? "❌" :
        step.status === "running" ? "🔄" : "⏳";
      return '<p class="deploy-log-line status-' + escapeHtml(step.status) + '">' + glyph + " " +
        escapeHtml(step.label || step.step) + (step.message ? " — " + escapeHtml(step.message) : "") + "</p>";
    }).join("");

    var finalStep = progress[progress.length - 1];
    var result = finalStep && finalStep.result;
    if (!result || !resultEl) return;
    var html = "<p><strong>VM:</strong> " + escapeHtml(result.vm_ip) +
      " · <strong>Ổ:</strong> " + escapeHtml(result.device) +
      " · <strong>Profile:</strong> " + escapeHtml(result.profile) + "</p>";
    if (result.disk_info) html += '<p class="hint">lsblk: ' + escapeHtml(result.disk_info) + "</p>";
    if (result.knee) {
      html += "<p><strong>Điểm knee đọc:</strong> iodepth=" + result.knee.iodepth +
        " · " + fmt(result.knee.iops, 0) + " IOPS · p99 " + fmt(result.knee.latency_p99_ms, 2) + " ms</p>";
    } else {
      html += '<p class="hint">Chưa thấy điểm knee trong dải tải đã đo; mức cao nhất chỉ là cận dưới.</p>';
    }
    html += '<div class="table-wrap"><table><thead><tr><th>iodepth</th><th>Số lần đo</th><th>IOPS median</th><th>MiB/s</th><th>Độ lệch</th><th>Latency avg</th><th>p99</th></tr></thead><tbody>';
    (result.steps || []).forEach(function (row) {
      html += "<tr><td>" + row.iodepth + "</td><td>" + (row.sample_count || 0) + "/3</td><td>" + fmt(row.iops, 0) + "</td><td>" +
        fmt(row.bandwidth_mib_s, 2) + "</td><td>" + fmt(row.iops_cv_pct, 1) + "%</td><td>" +
        fmt(row.latency_avg_ms, 2) + " ms</td><td>" + fmt(row.latency_p99_ms, 2) + " ms</td></tr>";
    });
    html += "</tbody></table></div>";
    resultEl.innerHTML = html;
  }

  function poll() {
    fetch("/api/volumes/vm-perf/progress", { credentials: "same-origin" })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        render(data.progress);
        if (data.status === "EXECUTED" || data.status === "FAILED") {
          if (timer) clearInterval(timer);
          if (status === "APPROVED") window.location.reload();
        }
      }).catch(function () { /* retry on the next poll */ });
  }

  poll();
  if (status === "APPROVED") timer = setInterval(poll, 3000);
})();
