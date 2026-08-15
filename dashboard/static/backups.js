(function () {
  var POLL_INTERVAL_MS = 3000;

  var idleEl = document.getElementById("backup-progress-idle");
  var detailEl = document.getElementById("backup-progress-detail");
  var titleEl = document.getElementById("backup-progress-title");
  var barEl = document.getElementById("backup-progress-bar");
  var barFillEl = document.getElementById("backup-progress-bar-fill");
  var pctEl = document.getElementById("backup-progress-pct");
  var speedEl = document.getElementById("backup-progress-speed");
  var etaEl = document.getElementById("backup-progress-eta");

  var ACTION_LABEL = {
    rbd_backup_run: "Backup RBD",
    backup_metadata_run: "Backup metadata cụm",
    restore_drill_execute: "RestoreDrill",
    restore_rbd_image_to_production: "Khôi phục volume",
  };

  function fmt(v, digits) {
    return typeof v === "number" ? v.toFixed(digits == null ? 1 : digits) : "—";
  }

  function fmtEta(seconds) {
    if (typeof seconds !== "number" || seconds < 0) return "—";
    var m = Math.floor(seconds / 60);
    var s = Math.round(seconds % 60);
    return m > 0 ? m + "p " + s + "s" : s + "s";
  }

  function renderIdle() {
    if (idleEl) idleEl.hidden = false;
    if (detailEl) detailEl.hidden = true;
  }

  function renderRunning(actionId, step) {
    if (idleEl) idleEl.hidden = true;
    if (detailEl) detailEl.hidden = false;
    if (titleEl) titleEl.textContent = ACTION_LABEL[actionId] || actionId;

    var pct = typeof step.pct === "number" ? step.pct : 0;
    if (barEl) barEl.setAttribute("aria-valuenow", String(pct));
    if (barFillEl) barFillEl.style.width = pct + "%";
    if (pctEl) pctEl.textContent = fmt(pct, 0);
    if (speedEl) speedEl.textContent = fmt(step.speed_mbps, 2);
    if (etaEl) etaEl.textContent = fmtEta(step.eta_seconds);
  }

  function poll() {
    fetch("/api/backups/progress", { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (!data.action_id || !data.progress || !data.progress.length) {
          renderIdle();
          return;
        }
        renderRunning(data.action_id, data.progress[0]);
      })
      .catch(function () {
        // Transient network hiccup — next tick retries, same posture as
        // volume_perf_sweep.js's own poll loop.
      });
  }

  poll();
  // Pause polling while the tab is hidden; refresh once on return.
  setInterval(function () { if (document.hidden) return; poll(); }, POLL_INTERVAL_MS);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) poll(); });

  function postRunNow(url, payload, button) {
    button.disabled = true;
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
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
        button.disabled = false;
        window.alert(err.message || "Không tạo được job backup");
      });
  }

  Array.prototype.forEach.call(document.querySelectorAll(".btn-backup-now"), function (btn) {
    btn.addEventListener("click", function () {
      var pool = btn.getAttribute("data-pool");
      var image = btn.getAttribute("data-image");
      if (window.confirm("Chạy backup ngay cho " + pool + "/" + image + "?")) {
        postRunNow("/backups/run-now", { pool: pool, image: image }, btn);
      }
    });
  });

  var metadataBtn = document.getElementById("btn-backup-metadata-now");
  if (metadataBtn) {
    metadataBtn.addEventListener("click", function () {
      if (window.confirm("Chạy backup metadata cụm ngay?")) {
        postRunNow("/backups/metadata/run-now", {}, metadataBtn);
      }
    });
  }

  // --- Khôi phục 1 volume (Story 9.7, restore_rbd_image_to_production) ---

  Array.prototype.forEach.call(document.querySelectorAll(".btn-restore-image"), function (btn) {
    btn.addEventListener("click", function () {
      var pool = btn.getAttribute("data-pool");
      var image = btn.getAttribute("data-image");
      if (!window.confirm("Xác nhận đề xuất khôi phục " + pool + "/" + image + " từ backup? Thao tác này sẽ GHI ĐÈ dữ liệu hiện tại của image này sau khi được duyệt.")) {
        return;
      }
      btn.disabled = true;
      fetch("/backups/restore/propose", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pool: pool, image: image })
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
          btn.disabled = false;
          window.alert(err.message || "Không tạo được đề xuất khôi phục");
        });
    });
  });
})();
