(function () {
  var form = document.getElementById("delete-form");
  var initialStateEl = document.getElementById("delete-initial-state");
  if (!initialStateEl) {
    return; // not on /delete-cluster
  }

  var initialState = JSON.parse(initialStateEl.textContent || "{}");
  var clusterName = initialState.cluster_name || "Cụm mặc định";

  var POLL_INTERVAL_MS = 2500;
  var TERMINAL_STATUSES = ["EXECUTED", "FAILED"];
  var STATUS_GLYPH = { pending: "⏳", running: "🔄", done: "✅", failed: "❌" };

  function pad2(n) { return String(n).padStart(2, "0"); }
  function nowClock() {
    var d = new Date();
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  // --- Wipe-OSD-disk checkbox -> reveal per-node disk inputs -------------

  var wipeCheckbox = document.getElementById("dc-wipe-osd");
  var osdDiskSection = document.getElementById("dc-osd-disk-section");
  var wipeWarning = document.getElementById("dc-wipe-warning");
  if (wipeCheckbox && osdDiskSection) {
    wipeCheckbox.addEventListener("change", function () {
      osdDiskSection.hidden = !wipeCheckbox.checked;
      if (wipeWarning) wipeWarning.hidden = !wipeCheckbox.checked;
    });
  }

  // --- Propose submit -----------------------------------------------------

  var errorEl = document.getElementById("dc-error");

  // --- Destructive proposal modal: exact name + five-second pause --------

  var modal = document.getElementById("dc-confirm-modal");
  var modalInput = document.getElementById("dc-cluster-confirmation");
  var modalSubmit = document.getElementById("dc-modal-submit");
  var modalCancel = document.getElementById("dc-modal-cancel");
  var modalClose = document.getElementById("dc-modal-close");
  var modalError = document.getElementById("dc-modal-error");
  var countdownBox = document.getElementById("dc-countdown-box");
  var countdownEl = document.getElementById("dc-countdown");
  var modalTimer = null;
  var modalPayload = null;
  var countdownFinished = false;

  function closeModal() {
    if (!modal) return;
    modal.hidden = true;
    if (modalTimer) { clearInterval(modalTimer); modalTimer = null; }
    modalPayload = null;
    countdownFinished = false;
    if (modalInput) modalInput.value = "";
    if (modalSubmit) modalSubmit.disabled = true;
    if (countdownBox) countdownBox.hidden = true;
    if (modalError) { modalError.hidden = true; modalError.textContent = ""; }
  }

  function openModal(payload) {
    if (!modal || !modalInput) return;
    modalPayload = payload;
    modal.hidden = false;
    modalInput.value = "";
    modalInput.focus();
  }

  function updateModalState() {
    var exact = modalInput && modalInput.value === clusterName;
    if (!exact) {
      countdownFinished = false;
      if (modalTimer) { clearInterval(modalTimer); modalTimer = null; }
      if (countdownBox) countdownBox.hidden = true;
      if (modalSubmit) modalSubmit.disabled = true;
      return;
    }
    if (countdownFinished || modalTimer) return;
    var remaining = 5;
    countdownFinished = false;
    if (countdownEl) countdownEl.textContent = String(remaining);
    if (countdownBox) countdownBox.hidden = false;
    if (modalSubmit) modalSubmit.disabled = true;
    modalTimer = setInterval(function () {
      remaining -= 1;
      if (countdownEl) countdownEl.textContent = String(Math.max(remaining, 0));
      if (remaining <= 0) {
        clearInterval(modalTimer);
        modalTimer = null;
        countdownFinished = true;
        if (modalSubmit) modalSubmit.disabled = false;
      }
    }, 1000);
  }

  function submitProposal() {
    if (!modalPayload || !modalSubmit || modalSubmit.disabled) return;
    modalSubmit.disabled = true;
    if (modalError) { modalError.hidden = true; modalError.textContent = ""; }
    fetch("/delete-cluster/propose", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({}, modalPayload, { confirmation: clusterName }))
    })
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (data) {
            throw new Error(data.detail || "HTTP " + response.status);
          });
        }
        return response.json();
      })
      .then(function () { window.location.reload(); })
      .catch(function (err) {
        if (modalError) {
          modalError.textContent = err.message || "Không tạo được đề xuất xoá cụm";
          modalError.hidden = false;
        }
        if (modalSubmit) modalSubmit.disabled = false;
      });
  }

  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (errorEl) { errorEl.hidden = true; errorEl.textContent = ""; }
      var wipeOsdDisks = !!(wipeCheckbox && wipeCheckbox.checked);
      var osdDisks = {};
      if (wipeOsdDisks) {
        Array.prototype.forEach.call(document.querySelectorAll(".dc-osd-disk-input"), function (input) {
          var raw = input.value.trim();
          osdDisks[input.dataset.ip] = raw
            ? raw.split(",").map(function (d) { return d.trim(); }).filter(function (d) { return d.length > 0; })
            : [];
        });
      }
      openModal({ wipe_osd_disks: wipeOsdDisks, osd_disks: osdDisks });
    });
  }
  if (modalInput) modalInput.addEventListener("input", updateModalState);
  if (modalSubmit) modalSubmit.addEventListener("click", submitProposal);
  if (modalCancel) modalCancel.addEventListener("click", closeModal);
  if (modalClose) modalClose.addEventListener("click", closeModal);
  if (modal) modal.addEventListener("click", function (event) { if (event.target === modal) closeModal(); });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape" && modal && !modal.hidden) closeModal(); });

  // --- Best-effort impact summary from existing inventory APIs ------------

  var impactStatus = document.getElementById("dc-impact-status");
  var impactNodes = {};
  Array.prototype.forEach.call(document.querySelectorAll("[data-impact]"), function (node) {
    impactNodes[node.dataset.impact] = node;
  });
  function setImpact(key, value) { if (impactNodes[key]) impactNodes[key].textContent = value; }
  function getJson(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (response) {
      if (!response.ok) throw new Error("HTTP " + response.status);
      return response.json();
    });
  }
  function formatGiB(bytes) {
    var value = Number(bytes || 0) / 1073741824;
    return value >= 100 ? Math.round(value).toLocaleString("vi-VN") : value.toFixed(1);
  }
  function poolName(row) { return row && (row.name || row.pool_name || row.poolname || row.pool); }
  function loadImpact() {
    if (!impactStatus || !Object.keys(impactNodes).length) return;
    getJson("/api/pools").then(function (poolBody) {
      var poolRows = Array.isArray(poolBody.items) ? poolBody.items : Array.isArray(poolBody.data) ? poolBody.data : [];
      var pools = poolRows.map(poolName).filter(function (name) { return name; });
      setImpact("pools", pools.length.toLocaleString("vi-VN"));
      return Promise.all(pools.map(function (pool) {
        return getJson("/api/volumes/" + encodeURIComponent(pool) + "/inventory").catch(function () { return null; });
      })).then(function (volumeBodies) {
        var count = 0;
        var bytes = 0;
        volumeBodies.forEach(function (body) {
          if (!body || !body.summary) return;
          count += Number(body.summary.image_count || 0);
          bytes += Number(body.summary.provisioned_size || 0);
        });
        setImpact("volumes", count.toLocaleString("vi-VN"));
        setImpact("volume-size", formatGiB(bytes));
      });
    }).catch(function () {
      setImpact("pools", "—");
      setImpact("volumes", "—");
      setImpact("volume-size", "—");
    }).then(function () {
      return getJson("/api/object-storage/buckets").then(function (bucketBody) {
        var total = Number(bucketBody.total || 0);
        setImpact("buckets", total.toLocaleString("vi-VN"));
        var pageCount = Math.min(Number(bucketBody.page_count || 1), 100);
        var pages = [bucketBody];
        var requests = [];
        for (var page = 2; page <= pageCount; page += 1) {
          requests.push(getJson("/api/object-storage/buckets?page=" + page));
        }
        return Promise.all(requests).then(function (rest) {
          pages = pages.concat(rest);
          var objects = 0;
          var complete = true;
          pages.forEach(function (body) {
            (body.items || []).forEach(function (item) {
              if (item.num_objects == null) complete = false;
              objects += Number(item.num_objects || 0);
            });
          });
          setImpact("objects", complete ? objects.toLocaleString("vi-VN") : "—");
        });
      }).catch(function () {
        setImpact("buckets", "—");
        setImpact("objects", "—");
      });
    }).then(function () {
      impactStatus.textContent = "Cập nhật từ inventory hiện tại";
    });
  }
  loadImpact();

  // --- Confirm-text gate on the Duyệt button ------------------------------

  var confirmInput = document.getElementById("dc-confirm-input");
  var approveBtn = document.getElementById("dc-approve-btn");
  var confirmHidden = document.getElementById("dc-confirm-hidden");
  if (confirmInput && approveBtn) {
    var expected = initialState.confirm_text || "";
    confirmInput.addEventListener("input", function () {
      approveBtn.disabled = confirmInput.value !== expected;
      if (confirmHidden) confirmHidden.value = confirmInput.value;
    });
  }

  // --- Progress polling + terminal log rendering -------------------------

  var logBox = document.getElementById("dc-log-box");
  var progressBarFill = document.getElementById("dc-progress-bar-fill");
  var progressBar = document.getElementById("dc-progress-bar");
  var progressLabel = document.getElementById("dc-progress-label");
  var logTitle = document.getElementById("dc-log-title");
  var clearBtn = document.getElementById("dc-log-clear");
  var copyBtn = document.getElementById("dc-log-copy");

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
      // 2026-07-28 fix: same bug/fix as dashboard/static/deploy_cluster.js
      // ::renderProgress — this used to be nowClock() unconditionally,
      // which rewrote EVERY line's timestamp to "now" on every poll tick
      // (this function rebuilds the whole log box from scratch each
      // call), so an already-finished step's displayed time never
      // actually froze. finished_at_display is computed server-side
      // (dashboard/routes/delete_cluster.py) from that step's own real,
      // frozen finished_at — only the step CURRENTLY running still ticks
      // live.
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
      else if (status === "APPROVED") logTitle.textContent = "● ĐANG XOÁ...";
    }
  }

  var pollTimer = null;

  function pollOnce() {
    fetch("/delete-cluster/progress", { credentials: "same-origin" })
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
