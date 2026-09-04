(function () {
  var POLL_INTERVAL_MS = 3000;

  // Backup is a single-panel workspace: keep the page compact even when
  // history, anomaly details, or digest summaries contain large payloads.
  var backupTabs = document.querySelectorAll("[data-backup-tab]");
  var backupPanels = document.querySelectorAll("[data-backup-panel]");

  function activateBackupTab(name) {
    Array.prototype.forEach.call(backupTabs, function (tab) {
      var active = tab.getAttribute("data-backup-tab") === name;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", active ? "true" : "false");
    });
    Array.prototype.forEach.call(backupPanels, function (panel) {
      panel.hidden = panel.getAttribute("data-backup-panel") !== name;
    });
  }

  Array.prototype.forEach.call(backupTabs, function (tab) {
    tab.addEventListener("click", function () {
      activateBackupTab(tab.getAttribute("data-backup-tab"));
    });
  });
  if (backupTabs.length) activateBackupTab("protection");

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
    restore_rbd_image_as_new: "Khôi phục thành volume mới",
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
  setInterval(poll, POLL_INTERVAL_MS);

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

  var deleteAllDigestsBtn = document.getElementById("btn-delete-all-backup-digests");
  if (deleteAllDigestsBtn) {
    deleteAllDigestsBtn.addEventListener("click", function () {
      if (!window.confirm("Xóa vĩnh viễn toàn bộ thông báo Digest của cluster đang chọn?")) return;
      deleteAllDigestsBtn.disabled = true;
      fetch("/backups/digests/delete-all", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" }
      })
        .then(function (response) {
          return response.json().then(function (data) {
            if (!response.ok) throw new Error(data.detail || "HTTP " + response.status);
            return data;
          });
        })
        .then(function (data) {
          window.alert("Đã xóa " + (data.deleted_count || 0) + " thông báo Digest.");
          window.location.reload();
        })
        .catch(function (err) {
          deleteAllDigestsBtn.disabled = false;
          window.alert(err.message || "Không thể xóa thông báo Digest");
        });
    });
  }

  // Safe default: restore into a new image and leave production untouched.

  Array.prototype.forEach.call(document.querySelectorAll(".btn-restore-image"), function (btn) {
    btn.addEventListener("click", async function () {
      var pool = btn.getAttribute("data-pool");
      var image = btn.getAttribute("data-image");
      btn.disabled = true;
      var recoveryPointId = "";
      try {
        var pointsResponse = await fetch(
          "/api/backups/recovery-points?pool=" + encodeURIComponent(pool) + "&image=" + encodeURIComponent(image),
          { credentials: "same-origin" }
        );
        var pointsBody = await pointsResponse.json();
        if (!pointsResponse.ok) throw new Error(pointsBody.detail || "Không tải được recovery point");
        var points = pointsBody.recovery_points || [];
        if (!points.length) throw new Error("Không có recovery point hợp lệ để khôi phục");
        var choices = points.map(function (point, index) {
          return (index + 1) + ". " + new Date(point.created_at).toLocaleString("vi-VN") +
            " · " + point.job_type + " · chain " + point.chain_length + " · target " +
            (point.backup_target_slot || "—");
        }).join("\n");
        var selected = window.prompt("Chọn recovery point (nhập số):\n\n" + choices, "1");
        if (selected === null) { btn.disabled = false; return; }
        var selectedIndex = Number(selected) - 1;
        if (!Number.isInteger(selectedIndex) || !points[selectedIndex]) {
          throw new Error("Recovery point đã chọn không hợp lệ");
        }
        recoveryPointId = points[selectedIndex].job_id;
      } catch (err) {
        btn.disabled = false;
        window.alert(err.message || "Không tải được recovery point");
        return;
      }
      var destPool = window.prompt("Pool đích cho volume khôi phục:", pool);
      if (destPool === null) { btn.disabled = false; return; }
      var destImage = window.prompt("Tên volume mới:", image + "-restored");
      if (destImage === null) { btn.disabled = false; return; }
      destPool = destPool.trim();
      destImage = destImage.trim();
      if (!destPool || !destImage || !window.confirm("Khôi phục " + pool + "/" + image + " thành volume mới " + destPool + "/" + destImage + "? Volume nguồn sẽ không bị thay đổi.")) {
        btn.disabled = false;
        return;
      }
      fetch("/backups/restore-as-new/propose", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pool: pool, image: image, dest_pool: destPool, dest_image: destImage,
          recovery_point_job_id: recoveryPointId })
      })
        .then(function (response) {
          if (!response.ok) {
            return response.json().then(function (data) {
              var detail = data.detail;
              if (detail && typeof detail === "object") {
                detail = detail.message + ((detail.blockers || []).length ? "\nBlockers: " + detail.blockers.join(", ") : "");
              }
              throw new Error(detail || "HTTP " + response.status);
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
