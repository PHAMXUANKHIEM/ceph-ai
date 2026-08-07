(function () {
  var form = document.getElementById("restore-form");
  var initialStateEl = document.getElementById("restore-initial-state");
  if (!initialStateEl) {
    return; // not on /restore-cluster
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

  // --- Node table -----------------------------------------------------

  var nodeRowsEl = document.getElementById("node-rows");
  var addNodeBtn = document.getElementById("rf-add-node");

  function addNodeRow(ip) {
    if (!nodeRowsEl) return;
    var row = document.createElement("tr");
    row.innerHTML =
      '<td><input type="text" class="node-ip" placeholder="10.20.1.112" value="' + (ip || "") + '"></td>' +
      '<td><input type="checkbox" class="node-role" value="mon"></td>' +
      '<td><input type="checkbox" class="node-role" value="mgr"></td>' +
      '<td><input type="checkbox" class="node-role node-role-osd" value="osd"></td>' +
      '<td><input type="checkbox" class="node-role" value="mds"></td>' +
      '<td><input type="checkbox" class="node-role" value="rgw"></td>' +
      '<td><input type="text" class="node-osd-disk" placeholder="/dev/vdc, /dev/vdd" disabled></td>' +
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
        var rawDisks = diskInput ? diskInput.value.trim() : "";
        node.osd_disks = rawDisks
          ? rawDisks.split(",").map(function (d) { return d.trim(); }).filter(function (d) { return d.length > 0; })
          : [];
      }
      nodes.push(node);
    });
    return nodes;
  }

  // --- Version picker: chọn dòng release rồi chọn phiên bản -------------

  var versionInput = document.getElementById("rf-version");
  var codenameSelect = document.getElementById("rf-codename");
  var versionSelect = document.getElementById("rf-version-select");
  var versionsByCodenameEl = document.getElementById("versions-by-codename-data");
  var errorEl = document.getElementById("rf-error");

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

      var payload = {
        version: versionInput ? versionInput.value.trim() : "",
        nodes: collectNodes(),
        public_network: document.getElementById("rf-public-network").value.trim(),
        cluster_network: document.getElementById("rf-cluster-network").value.trim(),
        osd_pool_default_size: parseInt(document.getElementById("rf-pool-size").value, 10) || 3,
        osd_pool_default_min_size: parseInt(document.getElementById("rf-pool-min-size").value, 10) || 2
      };

      if (!window.confirm("Xác nhận đề xuất KHÔI PHỤC CỤM SAU THẢM HỌA? Thao tác này sẽ dựng cụm mới và ghi đè dữ liệu RBD trên các node vừa điền bằng dữ liệu từ backup.")) {
        return;
      }

      fetch("/restore-cluster/propose", {
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
            errorEl.textContent = err.message || "Không tạo được đề xuất khôi phục";
            errorEl.hidden = false;
          }
        });
    });
  }

  // --- Progress polling + terminal log rendering -------------------------

  var logBox = document.getElementById("rf-log-box");
  var progressBarFill = document.getElementById("rf-progress-bar-fill");
  var progressBar = document.getElementById("rf-progress-bar");
  var progressLabel = document.getElementById("rf-progress-label");
  var logTitle = document.getElementById("rf-log-title");
  var clearBtn = document.getElementById("rf-log-clear");
  var copyBtn = document.getElementById("rf-log-copy");

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
      else if (status === "APPROVED") logTitle.textContent = "● ĐANG KHÔI PHỤC...";
    }
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = String(text == null ? "" : text);
    return div.innerHTML;
  }

  var pollTimer = null;

  function pollOnce() {
    fetch("/restore-cluster/progress", { credentials: "same-origin" })
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
