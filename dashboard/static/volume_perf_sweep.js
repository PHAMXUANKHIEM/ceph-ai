(function () {
  var panel = document.getElementById("perf-sweep-panel");
  if (!panel) {
    return; // /volumes with no pool selected, or not on this page at all
  }

  var pool = panel.dataset.pool;
  var initialStatus = panel.dataset.status || null;
  var POLL_INTERVAL_MS = 3000;
  var TERMINAL_STATUSES = ["EXECUTED", "FAILED"];
  var STATUS_GLYPH = { pending: "⏳", running: "🔄", done: "✅", failed: "❌", skipped: "⏭" };

  var runBtn = document.getElementById("perf-sweep-run-btn");
  var errorEl = document.getElementById("perf-sweep-error");
  var progressEl = document.getElementById("perf-sweep-progress");
  var resultEl = document.getElementById("perf-sweep-result");

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = String(text == null ? "" : text);
    return div.innerHTML;
  }

  function fmt(v, digits) {
    return typeof v === "number" ? v.toFixed(digits == null ? 1 : digits) : "—";
  }

  function setError(message) {
    if (!errorEl) return;
    errorEl.hidden = !message;
    errorEl.textContent = message || "";
  }

  if (runBtn) {
    runBtn.addEventListener("click", function () {
      runBtn.disabled = true;
      setError(null);
      fetch("/volumes/" + encodeURIComponent(pool) + "/perf-sweep/propose", {
        method: "POST",
        credentials: "same-origin"
      })
        .then(function (response) {
          if (!response.ok) {
            return response.json().then(function (body) {
              throw new Error(body.detail || "HTTP " + response.status);
            });
          }
          return response.json();
        })
        .then(function () {
          window.location.reload(); // server-renders the "Chờ duyệt" approve/reject buttons
        })
        .catch(function (err) {
          runBtn.disabled = false;
          setError(err.message);
        });
    });
  }

  /* ---------- live progress (while APPROVED) ---------- */

  function renderProgress(status, progress) {
    if (!progressEl) return;
    if (!progress || !progress.length) {
      progressEl.hidden = true;
      return;
    }
    progressEl.hidden = false;
    progressEl.innerHTML = "";
    progress.forEach(function (step) {
      var glyph = STATUS_GLYPH[step.status] || "•";
      var line = document.createElement("p");
      line.className = "deploy-log-line status-" + step.status;
      line.innerHTML = glyph + " " + escapeHtml(step.label || step.step);
      if (step.message) line.innerHTML += " — " + escapeHtml(step.message);
      progressEl.appendChild(line);
      (step.hosts || []).forEach(function (h) {
        var hostGlyph = STATUS_GLYPH[h.status] || "•";
        var hostLine = document.createElement("p");
        hostLine.className = "deploy-log-line status-" + h.status;
        hostLine.style.marginLeft = "1.5em";
        hostLine.innerHTML = hostGlyph + " " + escapeHtml(h.host) + (h.message ? " — " + escapeHtml(h.message) : "");
        progressEl.appendChild(hostLine);
      });
    });
    progressEl.scrollTop = progressEl.scrollHeight;
  }

  var pollTimer = null;

  function pollProgress() {
    fetch("/api/volumes/" + encodeURIComponent(pool) + "/perf-sweep/progress", { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        renderProgress(data.status, data.progress);
        if (data.status && TERMINAL_STATUSES.indexOf(data.status) !== -1) {
          if (pollTimer) clearInterval(pollTimer);
          window.location.reload(); // flips back to the propose-button state + refreshes the result panel
        }
      })
      .catch(function () {
        // Transient network hiccup — next tick retries.
      });
  }

  if (initialStatus === "APPROVED") {
    pollProgress();
    pollTimer = setInterval(pollProgress, POLL_INTERVAL_MS);
  }

  /* ---------- latest completed result ---------- */

  function renderResult(sweep) {
    if (!resultEl) return;
    if (!sweep) {
      resultEl.innerHTML = '<p class="hint">Chưa có lượt đo hiệu năng nào cho pool này.</p>';
      return;
    }
    if (sweep.status === "FAILED") {
      resultEl.innerHTML =
        '<p class="error">Lần đo gần nhất thất bại' +
        (sweep.error_message ? ": " + escapeHtml(sweep.error_message) : "") +
        "</p>";
      return;
    }
    if (sweep.status === "RUNNING") {
      resultEl.innerHTML = "";
      return; // the live progress block above already covers this state
    }

    var html = "";
    if (sweep.knee) {
      html +=
        '<p><strong>Điểm bão hoà (usable ceiling):</strong> iodepth=' + sweep.knee.iodepth +
        " · IOPS " + fmt(sweep.knee.iops, 0) +
        " · latency avg " + fmt(sweep.knee.latency_avg_ms, 2) + "ms" +
        " · p99 " + fmt(sweep.knee.latency_p99_ms, 2) + "ms</p>";
    } else {
      html += '<p class="hint">Chưa bão hoà trong dải iodepth đã quét — cụm còn dư sức, số liệu dưới đây là mức sàn, chưa phải trần.</p>';
    }

    if (sweep.steps && sweep.steps.length) {
      html += '<div class="table-wrap"><table><thead><tr><th>iodepth</th><th>IOPS</th><th>Latency avg</th><th>Latency p99</th></tr></thead><tbody>';
      sweep.steps.forEach(function (s) {
        var isKnee = sweep.knee && s.iodepth === sweep.knee.iodepth;
        html +=
          "<tr" + (isKnee ? ' class="progress-item-failed"' : "") + "><td>" + s.iodepth + "</td><td>" +
          fmt(s.iops, 0) + "</td><td>" + fmt(s.latency_avg_ms, 2) + "ms</td><td>" +
          fmt(s.latency_p99_ms, 2) + "ms</td></tr>";
      });
      html += "</tbody></table></div>";
    }

    if (sweep.qos_notes) {
      html += "<p class=\"hint\"><strong>QoS:</strong> " + escapeHtml(sweep.qos_notes) + "</p>";
    }
    if (sweep.bottleneck_notes) {
      html +=
        '<details><summary class="hint">Chi tiết chẩn đoán nút thắt (iostat / ceph osd perf)</summary>' +
        "<pre>" + escapeHtml(sweep.bottleneck_notes) + "</pre></details>";
    }

    resultEl.innerHTML = html;
  }

  fetch("/api/volumes/" + encodeURIComponent(pool) + "/perf-sweep/latest", { credentials: "same-origin" })
    .then(function (response) { return response.ok ? response.json() : null; })
    .then(function (data) { if (data) renderResult(data.sweep); })
    .catch(function () { /* result panel is a convenience, not required */ });
})();
