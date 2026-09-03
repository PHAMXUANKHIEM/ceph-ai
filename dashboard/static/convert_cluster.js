(function () {
  var form = document.getElementById("convert-form");
  var initialStateEl = document.getElementById("convert-initial-state");
  if (!initialStateEl) {
    return; // not on /convert-cluster
  }

  var initialState = JSON.parse(initialStateEl.textContent || "{}");

  var POLL_INTERVAL_MS = 2500;
  var TERMINAL_STATUSES = ["EXECUTED", "FAILED"];
  var STATUS_GLYPH = { pending: "⏳", running: "🔄", done: "✅", failed: "❌" };

  function pad2(n) { return String(n).padStart(2, "0"); }
  function nowClock() {
    var d = new Date();
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  // --- Propose submit -----------------------------------------------------

  var errorEl = document.getElementById("cc-error");

  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (errorEl) { errorEl.hidden = true; errorEl.textContent = ""; }

      fetch("/convert-cluster/propose", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({})
      })
        .then(function (response) {
          if (!response.ok) {
            return response.json().then(function (data) {
              throw new Error(data.detail || "HTTP " + response.status);
            });
          }
          return response.json();
        })
        .then(function () {
          window.location.reload();
        })
        .catch(function (err) {
          if (errorEl) {
            errorEl.textContent = err.message || "Không tạo được đề xuất chuyển đổi";
            errorEl.hidden = false;
          }
        });
    });
  }

  // --- Confirm-text gate on the Duyệt button ------------------------------

  var confirmInput = document.getElementById("cc-confirm-input");
  var approveBtn = document.getElementById("cc-approve-btn");
  var confirmHidden = document.getElementById("cc-confirm-hidden");
  if (confirmInput && approveBtn) {
    var expected = initialState.confirm_text || "";
    confirmInput.addEventListener("input", function () {
      approveBtn.disabled = confirmInput.value !== expected;
      if (confirmHidden) confirmHidden.value = confirmInput.value;
    });
  }

  // --- Progress polling + terminal log rendering -------------------------

  var logBox = document.getElementById("cc-log-box");
  var progressBarFill = document.getElementById("cc-progress-bar-fill");
  var progressBar = document.getElementById("cc-progress-bar");
  var progressLabel = document.getElementById("cc-progress-label");
  var logTitle = document.getElementById("cc-log-title");
  var clearBtn = document.getElementById("cc-log-clear");
  var copyBtn = document.getElementById("cc-log-copy");

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = String(text == null ? "" : text);
    return div.innerHTML;
  }

  function renderProgress(status, progress) {
    if (!logBox) return; // PENDING_APPROVAL view has no log box (shows the plan instead)
    if (!progress || !progress.length) return;

    logBox.innerHTML = "";
    var runningStep = null;
    progress.forEach(function (step) {
      var glyph = STATUS_GLYPH[step.status] || "•";
      var line = document.createElement("p");
      line.className = "deploy-log-line status-" + step.status;
      // Same frozen-timestamp fix as deploy_cluster.js/delete_cluster.js —
      // finished_at_display is computed server-side
      // (dashboard/routes/convert_cluster.py) from that step's own real,
      // frozen finished_at; only the step CURRENTLY running still ticks
      // live via nowClock().
      var clockText = step.status === "running"
        ? nowClock()
        : (step.status === "done" || step.status === "failed") ? step.finished_at_display : null;
      var timeSpan = clockText ? "<span class=\"deploy-log-time\">[" + clockText + "]</span> " : "";
      line.innerHTML = timeSpan + glyph + " " + escapeHtml(step.label || step.step);
      if (step.message) {
        line.innerHTML += " — " + escapeHtml(step.message);
      }
      logBox.appendChild(line);
      if (step.hosts && step.hosts.length) {
        step.hosts.forEach(function (h) {
          var hostGlyph = STATUS_GLYPH[h.status] || "•";
          var hostLine = document.createElement("p");
          hostLine.className = "deploy-log-line status-" + h.status;
          hostLine.style.marginLeft = "1.5em";
          hostLine.innerHTML = hostGlyph + " " + escapeHtml(h.host) + (h.message ? " — " + escapeHtml(h.message) : "");
          logBox.appendChild(hostLine);
        });
      }
      if (step.status === "running") runningStep = step;
    });
    logBox.scrollTop = logBox.scrollHeight;

    var lastDone = progress.filter(function (s) { return s.status === "done"; }).pop();
    var pct = runningStep ? runningStep.pct : (lastDone ? lastDone.pct : 0);
    var failedStep = progress.filter(function (s) { return s.status === "failed"; })[0];
    if (failedStep) pct = failedStep.pct;

    if (progressBarFill) {
      progressBarFill.style.width = pct + "%";
      progressBarFill.classList.toggle("is-active", !!runningStep);
    }
    if (progressBar) progressBar.setAttribute("aria-valuenow", String(pct));
    if (progressLabel) {
      if (failedStep) {
        progressLabel.textContent = pct + "% — Lỗi ở bước: " + (failedStep.label || failedStep.step);
      } else if (runningStep) {
        progressLabel.textContent = pct + "% — " + (runningStep.label || runningStep.step);
      } else {
        progressLabel.textContent = pct + "%";
      }
    }
    if (logTitle) {
      if (status === "EXECUTED") logTitle.textContent = "✅ Hoàn tất";
      else if (status === "FAILED") logTitle.textContent = "❌ Thất bại";
      else if (status === "APPROVED") logTitle.textContent = "● ĐANG CHUYỂN ĐỔI...";
    }
  }

  var pollTimer = null;

  function pollOnce() {
    fetch("/convert-cluster/progress", { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        renderProgress(data.status, data.progress);
        if (data.status && TERMINAL_STATUSES.indexOf(data.status) !== -1) {
          if (pollTimer) clearInterval(pollTimer);
          window.location.reload();
        }
      })
      .catch(function () {
        // Transient network hiccup — next tick retries.
      });
  }

  if (logBox) {
    renderProgress(initialState.status, initialState.progress);
    if (initialState.status === "APPROVED") {
      pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
      pollOnce();
    }
  }

  if (clearBtn && logBox) {
    clearBtn.addEventListener("click", function () { logBox.innerHTML = ""; });
  }
  if (copyBtn && logBox) {
    copyBtn.addEventListener("click", function () {
      var text = logBox.innerText || logBox.textContent || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text);
      }
    });
  }
})();
