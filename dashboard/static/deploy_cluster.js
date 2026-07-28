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
      '<td><input type="checkbox" class="node-role node-role-osd" value="osd"></td>' +
      '<td><input type="checkbox" class="node-role" value="mds"></td>' +
      '<td><input type="text" class="node-osd-disk" placeholder="/dev/vdc" disabled></td>' +
      '<td><button type="button" class="btn btn-sm btn-ghost node-remove">×</button></td>';
    row.querySelector(".node-remove").addEventListener("click", function () {
      row.remove();
    });
    var osdCheckbox = row.querySelector(".node-role-osd");
    var osdDiskInput = row.querySelector(".node-osd-disk");
    osdCheckbox.addEventListener("change", function () {
      osdDiskInput.disabled = !osdCheckbox.checked;
      if (!osdCheckbox.checked) osdDiskInput.value = "";
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
      var node = { ip: ip, roles: roles };
      if (roles.indexOf("osd") !== -1) {
        var diskInput = row.querySelector(".node-osd-disk");
        node.osd_disk = diskInput ? diskInput.value.trim() : "";
      }
      nodes.push(node);
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

  // --- Version picker: chọn dòng release rồi chọn phiên bản -------------
  // Two dependent <select>s that just FILL df-version (the real, still
  // directly-editable text input the form submits) — same "convenience
  // filler, never the only way in" role the old flat version-chip buttons
  // had, now organized by release line (nautilus/octopus/.../reef/...)
  // instead of one flat hardcoded list of 4.

  var versionInput = document.getElementById("df-version");
  var codenameSelect = document.getElementById("df-codename");
  var versionSelect = document.getElementById("df-version-select");
  var versionsByCodenameEl = document.getElementById("versions-by-codename-data");

  if (codenameSelect && versionSelect && versionsByCodenameEl) {
    var versionsByCodename = JSON.parse(versionsByCodenameEl.textContent || "{}");

    codenameSelect.addEventListener("change", function () {
      var versions = versionsByCodename[codenameSelect.value] || [];
      versionSelect.innerHTML = "";
      if (!codenameSelect.value || versions.length === 0) {
        versionSelect.disabled = true;
        var placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "— Chọn dòng release trước —";
        versionSelect.appendChild(placeholder);
        return;
      }
      versionSelect.disabled = false;
      // Newest point release of the line first — that's almost always
      // what an operator deploying/upgrading actually wants.
      for (var i = versions.length - 1; i >= 0; i--) {
        var option = document.createElement("option");
        option.value = versions[i];
        option.textContent = versions[i];
        versionSelect.appendChild(option);
      }
      if (versionInput) versionInput.value = versions[versions.length - 1];
    });

    versionSelect.addEventListener("change", function () {
      if (versionInput && versionSelect.value) versionInput.value = versionSelect.value;
    });
  }

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
      // 2026-07-28 fix: this used to be nowClock() unconditionally, which
      // rewrote EVERY line's timestamp to the browser's current clock on
      // every single poll tick (renderProgress rebuilds the whole log box
      // from scratch each time) — a step that had already finished kept
      // showing a drifting "now" instead of freezing at when it actually
      // finished. finished_at_display/started_at_display are computed
      // server-side (dashboard/routes/deploy_cluster.py) from the step's
      // own real, frozen timestamps — only the step CURRENTLY running still
      // ticks live (there is no "finished" moment for it yet to freeze at).
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
