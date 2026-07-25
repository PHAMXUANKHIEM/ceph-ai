(function () {
  var form = document.getElementById("deploy-form");
  var initialStateEl = document.getElementById("deploy-initial-state");
  if (!initialStateEl) {
    return; // not on /deploy-cluster
  }

  var initialState = JSON.parse(initialStateEl.textContent || "{}");
  var notYetSupported = initialState.not_yet_supported_methods || [];

  var POLL_INTERVAL_MS = 2500;
  var TERMINAL_STATUSES = ["EXECUTED", "FAILED"];

  var STATUS_GLYPH = { pending: "⏳", running: "🔄", done: "✅", failed: "❌" };

  function pad2(n) { return String(n).padStart(2, "0"); }
  function nowClock() {
    var d = new Date();
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  // --- Node table -----------------------------------------------------

  var nodeRowsEl = document.getElementById("node-rows");
  var addNodeBtn = document.getElementById("df-add-node");
  var nodeRowCount = 0;

  function addNodeRow(ip) {
    if (!nodeRowsEl) return;
    nodeRowCount += 1;
    var row = document.createElement("tr");
    row.innerHTML =
      '<td><input type="text" class="node-ip" placeholder="10.20.1.112" value="' + (ip || "") + '"></td>' +
      '<td><input type="checkbox" class="node-role" value="mon"></td>' +
      '<td><input type="checkbox" class="node-role" value="mgr"></td>' +
      '<td><input type="checkbox" class="node-role" value="osd"></td>' +
      '<td><input type="checkbox" class="node-role" value="mds"></td>' +
      '<td><button type="button" class="btn btn-sm btn-ghost node-remove">×</button></td>';
    row.querySelector(".node-remove").addEventListener("click", function () {
      row.remove();
    });
    nodeRowsEl.appendChild(row);
  }

  if (addNodeBtn) {
    addNodeBtn.addEventListener("click", function () { addNodeRow(); });
  }
  if (nodeRowsEl && nodeRowsEl.children.length === 0) {
    addNodeRow();
    addNodeRow();
    addNodeRow();
  }

  function collectNodes() {
    if (!nodeRowsEl) return [];
    var nodes = [];
    Array.prototype.forEach.call(nodeRowsEl.querySelectorAll("tr"), function (row) {
      var ip = row.querySelector(".node-ip").value.trim();
      if (!ip) return;
      var roles = [];
      Array.prototype.forEach.call(row.querySelectorAll(".node-role:checked"), function (cb) {
        roles.push(cb.value);
      });
      nodes.push({ ip: ip, roles: roles });
    });
    return nodes;
  }

  // --- Method radio -> rpm-path field + not-yet-supported note --------

  var rpmPathLabel = document.getElementById("df-rpm-path-label");
  var errorEl = document.getElementById("df-error");

  function currentMethod() {
    var checked = document.querySelector('input[name="method"]:checked');
    return checked ? checked.value : "cephadm";
  }

  function onMethodChange() {
    var method = currentMethod();
    if (rpmPathLabel) rpmPathLabel.hidden = method !== "rpm-local";
  }

  Array.prototype.forEach.call(document.querySelectorAll('input[name="method"]'), function (radio) {
    radio.addEventListener("change", onMethodChange);
  });
  onMethodChange();

  // --- Version quick-pick chips ----------------------------------------

  var versionInput = document.getElementById("df-version");
  Array.prototype.forEach.call(document.querySelectorAll(".version-chip"), function (chip) {
    chip.addEventListener("click", function () {
      if (versionInput) versionInput.value = chip.dataset.version;
    });
  });

  // --- Propose submit ---------------------------------------------------

  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (errorEl) { errorEl.hidden = true; errorEl.textContent = ""; }

      var method = currentMethod();
      if (notYetSupported.indexOf(method) !== -1) {
        if (errorEl) {
          errorEl.textContent = "Phương thức này chưa được hỗ trợ tự động — chọn cephadm.";
          errorEl.hidden = false;
        }
        return;
      }

      var payload = {
        version: versionInput ? versionInput.value.trim() : "",
        method: method,
        rpm_path: document.getElementById("df-rpm-path") ? document.getElementById("df-rpm-path").value.trim() : "",
        nodes: collectNodes(),
        public_network: document.getElementById("df-public-network").value.trim(),
        cluster_network: document.getElementById("df-cluster-network").value.trim(),
        osd_disk: document.getElementById("df-osd-disk").value.trim(),
        osd_pool_default_size: parseInt(document.getElementById("df-pool-size").value, 10) || 3,
        osd_pool_default_min_size: parseInt(document.getElementById("df-pool-min-size").value, 10) || 2
      };

      fetch("/deploy-cluster/propose", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
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
            errorEl.textContent = err.message || "Không tạo được đề xuất dựng cụm";
            errorEl.hidden = false;
          }
        });
    });
  }

  // --- Progress polling + terminal log rendering -------------------------

  var logBox = document.getElementById("df-log-box");
  var progressBarFill = document.getElementById("df-progress-bar-fill");
  var progressBar = document.getElementById("df-progress-bar");
  var progressLabel = document.getElementById("df-progress-label");
  var logTitle = document.getElementById("df-log-title");
  var clearBtn = document.getElementById("df-log-clear");
  var copyBtn = document.getElementById("df-log-copy");

  function renderProgress(status, progress) {
    if (!logBox) return; // PENDING_APPROVAL view has no log box (shows the plan instead)

    if (!progress || !progress.length) {
      return;
    }

    logBox.innerHTML = "";
    var runningStep = null;
    progress.forEach(function (step) {
      var glyph = STATUS_GLYPH[step.status] || "•";
      var line = document.createElement("p");
      line.className = "deploy-log-line status-" + step.status;
      var timeSpan = "<span class=\"deploy-log-time\">[" + nowClock() + "]</span> ";
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
      else if (status === "APPROVED") logTitle.textContent = "● ĐANG CÀI ĐẶT...";
    }
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = String(text == null ? "" : text);
    return div.innerHTML;
  }

  var pollTimer = null;

  function pollOnce() {
    fetch("/deploy-cluster/progress", { credentials: "same-origin" })
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
        // Transient network hiccup — next tick retries; no need to surface
        // this as a hard error the way a propose validation failure is.
      });
  }

  if (logBox) {
    renderProgress(initialState.status, initialState.progress);
    // Only poll (and auto-reload on completion) while a deploy is actually
    // in-flight (APPROVED — Worker picked it up, running now). Viewing an
    // already-resolved last_action's log (EXECUTED/FAILED, no pending
    // Action at all) must render once and stop — polling that case would
    // immediately see a terminal status again and reload the page forever.
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
