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

  var inventoryBody = document.getElementById("backup-inventory-body");
  var inventorySearch = document.getElementById("backup-inventory-search");
  var inventorySize = document.getElementById("backup-inventory-page-size");
  var inventoryStatus = document.getElementById("backup-inventory-status");
  var inventoryPageEl = document.getElementById("backup-inventory-page");
  var inventoryTotalEl = document.getElementById("backup-inventory-total");
  var inventoryPage = 1;
  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>\"']/g, function (char) {
      return {"&":"&amp;", "<":"&lt;", ">":"&gt;", "\"":"&quot;", "'":"&#39;"}[char];
    });
  }
  function loadInventory() {
    if (!inventoryBody) return;
    var params = new URLSearchParams({page: String(inventoryPage), page_size: inventorySize ? inventorySize.value : "25",
      search: inventorySearch ? inventorySearch.value : "", status: inventoryStatus ? inventoryStatus.value : ""});
    fetch("/api/backups/inventory?" + params.toString(), {credentials: "same-origin"})
      .then(function (response) { return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.detail || "HTTP " + response.status); return data;
      }); })
      .then(function (data) {
        var items = data.items || [];
        inventoryBody.innerHTML = items.length ? items.map(function (item) {
          return "<tr><td><code>" + escapeHtml((item.pool || "—") + "/" + (item.image || "—")) + "</code></td><td>" + escapeHtml(item.job_type) + "</td><td>" + escapeHtml(item.consistency_mode || "crash-consistent") + "</td><td>" + escapeHtml(item.backup_target_slot || "—") + "</td><td>" + escapeHtml(item.status) + "</td><td><code>" + escapeHtml(item.run_id) + "</code>" + (item.remote_key ? "<br><span class='hint'>" + escapeHtml(item.remote_key) + "</span>" : "") + "</td><td>" + escapeHtml(item.size_bytes || 0) + "</td><td><code>" + escapeHtml(item.sha256 || "—") + "</code></td><td>" + escapeHtml(item.created_at || "—") + "</td></tr>";
        }).join("") : "<tr><td colspan='9' class='hint'>Không tìm thấy artifact.</td></tr>";
        if (inventoryPageEl) inventoryPageEl.textContent = "Trang " + data.page + "/" + data.pages;
        if (inventoryTotalEl) inventoryTotalEl.textContent = data.total + " artifacts";
        var prev = document.getElementById("backup-inventory-prev"); var next = document.getElementById("backup-inventory-next");
        if (prev) prev.disabled = data.page <= 1; if (next) next.disabled = data.page >= data.pages;
      }).catch(function () { inventoryBody.innerHTML = "<tr><td colspan='9' class='hint'>Không tải được inventory.</td></tr>"; });
  }
  if (inventoryBody) {
    [inventorySearch, inventorySize, inventoryStatus].forEach(function (element) {
      if (element) element.addEventListener(element === inventorySearch ? "input" : "change", function () { inventoryPage = 1; loadInventory(); });
    });
    document.getElementById("backup-inventory-prev").addEventListener("click", function () { if (inventoryPage > 1) { inventoryPage--; loadInventory(); } });
    document.getElementById("backup-inventory-next").addEventListener("click", function () { inventoryPage++; loadInventory(); });
    loadInventory();
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-backup-open-tab]"), function (button) {
    button.addEventListener("click", function () {
      var target = button.getAttribute("data-backup-open-tab");
      if (target) activateBackupTab(target);
    });
  });

  var latestBackupEl = document.getElementById("backup-summary-latest");
  var latestBackupAbsoluteEl = document.getElementById("backup-summary-latest-absolute");

  function formatRelativeBackupTime(value) {
    if (!value) return "Chưa có";
    var timestamp = new Date(value).getTime();
    if (!Number.isFinite(timestamp)) return "Chưa có dữ liệu";
    var seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
    if (seconds < 60) return "Vừa xong";
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + " phút trước";
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + " giờ trước";
    var days = Math.floor(hours / 24);
    return days + " ngày trước";
  }

  function updateLatestBackup() {
    if (!latestBackupEl) return;
    var value = latestBackupEl.getAttribute("data-latest-at");
    latestBackupEl.textContent = formatRelativeBackupTime(value);
    if (latestBackupAbsoluteEl && value) {
      var date = new Date(value);
      if (Number.isFinite(date.getTime())) {
        latestBackupAbsoluteEl.textContent = date.toLocaleString("vi-VN");
      }
    }
  }

  updateLatestBackup();
  window.setInterval(updateLatestBackup, 60000);

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
    var progressCountEl = document.getElementById("backup-tab-progress-count");
    if (progressCountEl) progressCountEl.textContent = "0";
  }

  function renderRunning(actionId, step) {
    if (idleEl) idleEl.hidden = true;
    if (detailEl) detailEl.hidden = false;
    var progressCountEl = document.getElementById("backup-tab-progress-count");
    if (progressCountEl) progressCountEl.textContent = "1";
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

  Array.prototype.forEach.call(document.querySelectorAll(".btn-backup-retry"), function (btn) {
    btn.addEventListener("click", function () {
      var jobId = btn.getAttribute("data-job-id");
      if (!jobId || !window.confirm("Retry backup job thất bại này?")) return;
      btn.disabled = true;
      fetch("/backups/jobs/" + encodeURIComponent(jobId) + "/retry", {
        method: "POST", credentials: "same-origin", headers: {"Content-Type": "application/json"}
      }).then(function (response) { return response.json().then(function (data) {
        if (!response.ok) throw new Error(data.detail || "HTTP " + response.status);
        return data;
      }); }).then(function () { window.location.reload(); })
        .catch(function (error) { btn.disabled = false; window.alert(error.message || "Không tạo được retry"); });
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

  var restoreDrillBtn = document.getElementById("btn-restore-drill-now");
  if (restoreDrillBtn) {
    restoreDrillBtn.addEventListener("click", function () {
      var targetSelect = document.getElementById("restore-drill-target");
      var pointSelect = document.getElementById("restore-drill-recovery-point");
      var payload = {
        target_slot: targetSelect ? targetSelect.value : "",
        recovery_point_job_id: pointSelect ? pointSelect.value : ""
      };
      if (window.confirm("Chạy RestoreDrill vào scratch image đã cấu hình? Volume nguồn production sẽ không bị thay đổi.")) {
        postRunNow("/backups/restore-drill/run-now", payload, restoreDrillBtn);
      }
    });
  }

  var restoreDrillTarget = document.getElementById("restore-drill-target");
  var restoreDrillPoint = document.getElementById("restore-drill-recovery-point");
  if (restoreDrillTarget && restoreDrillPoint) {
    restoreDrillTarget.addEventListener("change", function () {
      var target = restoreDrillTarget.value;
      Array.prototype.forEach.call(restoreDrillPoint.options, function (option, index) {
        if (index === 0) { option.hidden = false; return; }
        option.hidden = !!target && option.getAttribute("data-target") !== target;
      });
      if (restoreDrillPoint.selectedOptions.length && restoreDrillPoint.selectedOptions[0].hidden) {
        restoreDrillPoint.value = "";
      }
    });
  }

  var deleteAllDigestsBtn = document.getElementById("btn-delete-all-backup-digests");
  if (deleteAllDigestsBtn) {
    deleteAllDigestsBtn.addEventListener("click", function () {
      var digestCount = document.querySelectorAll("[data-report-delete]").length;
      if (!window.confirm("Bạn có chắc muốn xóa tất cả " + digestCount + " bản digest?")) return;
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

  Array.prototype.forEach.call(document.querySelectorAll("[data-report-delete]"), function (button) {
    button.addEventListener("click", function (event) {
      event.preventDefault();
      event.stopPropagation();
      var digestId = button.getAttribute("data-report-delete");
      if (!digestId || !window.confirm("Xóa bản digest này khỏi cluster đang chọn?")) return;
      button.disabled = true;
      fetch("/backups/digests/" + encodeURIComponent(digestId) + "/delete", {
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
        .then(function () { window.location.reload(); })
        .catch(function (err) {
          button.disabled = false;
          window.alert(err.message || "Không thể xóa bản digest");
        });
    });
  });

  // Safe default: restore into a new image and leave production untouched.
  // Use an in-page dialog instead of prompt(): recovery chains and preflight
  // blockers must be visible before an operator creates a risky proposal.
  var restoreDialog = document.getElementById("backup-restore-dialog");
  var restoreForm = document.getElementById("backup-restore-form");
  var restorePointEl = document.getElementById("backup-restore-point");
  var restorePoolEl = document.getElementById("backup-restore-dest-pool");
  var restoreImageEl = document.getElementById("backup-restore-dest-image");
  var restoreSourceEl = document.getElementById("backup-restore-source");
  var restoreSubmit = document.getElementById("backup-restore-submit");
  var restoreError = document.getElementById("backup-restore-error");
  var restorePreflight = document.getElementById("backup-restore-preflight");
  var restorePreflightStatus = document.getElementById("backup-restore-preflight-status");
  var restorePreflightGrid = document.getElementById("backup-restore-preflight-grid");
  var restorePreflightDetail = document.getElementById("backup-restore-preflight-detail");
  var restoreContext = null;

  function restoreMessage(detail) {
    if (!detail) return "Không thể tạo đề xuất khôi phục";
    if (typeof detail === "string") return detail;
    var message = detail.message || "Restore preflight không đạt.";
    if (Array.isArray(detail.blockers) && detail.blockers.length) {
      message += "\nBlockers: " + detail.blockers.join(", ");
    }
    return message;
  }

  function clearRestoreFeedback() {
    if (restoreError) { restoreError.hidden = true; restoreError.textContent = ""; }
    if (restorePreflight) { restorePreflight.hidden = true; restorePreflight.classList.remove("is-failed"); }
    if (restorePreflightGrid) restorePreflightGrid.textContent = "";
    if (restorePreflightDetail) restorePreflightDetail.textContent = "";
  }

  function showRestoreError(message) {
    if (!restoreError) return;
    restoreError.hidden = false;
    restoreError.textContent = message;
  }

  function formatBytes(value) {
    if (typeof value !== "number" || value < 0) return "—";
    var units = ["B", "KiB", "MiB", "GiB", "TiB"];
    var index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return value.toFixed(index ? 1 : 0) + " " + units[index];
  }

  function renderRestorePreflight(data) {
    var passed = data && data.passed;
    if (!restorePreflight) return;
    restorePreflight.hidden = false;
    restorePreflight.classList.toggle("is-failed", !passed);
    if (restorePreflightStatus) {
      restorePreflightStatus.textContent = passed ? "Đạt — có thể tạo đề xuất" : "Không đạt";
      restorePreflightStatus.style.color = passed ? "#86efac" : "#fca5a5";
    }
    var destination = data && data.destination || {};
    var rows = [
      ["Nguồn", (data.source || {}).pool + "/" + (data.source || {}).image],
      ["Đích", destination.pool + "/" + destination.image],
      ["Dung lượng cần", formatBytes(data.required_bytes)],
      ["Dung lượng khả dụng", formatBytes(destination.max_available)],
      ["Số artifact", String((data.chain_job_ids || []).length)],
      ["Blocker", (data.blockers || []).length ? data.blockers.join(", ") : "Không có"]
    ];
    if (restorePreflightGrid) {
      rows.forEach(function (row) {
        var item = document.createElement("div");
        var label = document.createElement("span");
        var value = document.createElement("strong");
        label.textContent = row[0]; value.textContent = row[1] || "—";
        item.appendChild(label); item.appendChild(value); restorePreflightGrid.appendChild(item);
      });
    }
    if (restorePreflightDetail) restorePreflightDetail.textContent = JSON.stringify(data, null, 2);
  }

  async function openRestoreDialog(btn) {
    var pool = btn.getAttribute("data-pool");
    var image = btn.getAttribute("data-image");
    restoreContext = { pool: pool, image: image, button: btn, points: [] };
    btn.disabled = true;
    clearRestoreFeedback();
    if (restoreSourceEl) restoreSourceEl.textContent = pool + "/" + image;
    if (restorePoolEl) restorePoolEl.value = pool;
    if (restoreImageEl) restoreImageEl.value = image + "-restored";
    if (restorePointEl) restorePointEl.innerHTML = "<option>Đang tải recovery point…</option>";
    if (restoreDialog && typeof restoreDialog.showModal === "function") restoreDialog.showModal();
    try {
      var response = await fetch("/api/backups/recovery-points?pool=" + encodeURIComponent(pool) + "&image=" + encodeURIComponent(image), { credentials: "same-origin" });
      var body = await response.json();
      if (!response.ok) throw new Error(restoreMessage(body.detail));
      var points = body.recovery_points || [];
      if (!points.length) throw new Error("Không có recovery point hợp lệ để khôi phục");
      restoreContext.points = points;
      restorePointEl.innerHTML = "";
      points.forEach(function (point) {
        var option = document.createElement("option");
        option.value = point.job_id;
        option.textContent = new Date(point.created_at).toLocaleString("vi-VN") + " · " + point.job_type + " · chain " + point.chain_length + " · target " + (point.backup_target_slot || "—");
        restorePointEl.appendChild(option);
      });
    } catch (err) {
      showRestoreError(err.message || "Không tải được recovery point");
      if (restoreSubmit) restoreSubmit.disabled = true;
    }
  }

  function closeRestoreDialog() {
    if (restoreDialog && restoreDialog.open) restoreDialog.close();
    if (restoreContext && restoreContext.button) restoreContext.button.disabled = false;
    restoreContext = null;
  }

  Array.prototype.forEach.call(document.querySelectorAll(".btn-restore-image"), function (btn) {
    btn.addEventListener("click", function () { openRestoreDialog(btn); });
  });
  ["backup-restore-close", "backup-restore-cancel"].forEach(function (id) {
    var button = document.getElementById(id);
    if (button) button.addEventListener("click", closeRestoreDialog);
  });
  if (restoreDialog) restoreDialog.addEventListener("cancel", closeRestoreDialog);
  if (restoreForm) restoreForm.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (!restoreContext || !restorePointEl || !restorePoolEl || !restoreImageEl) return;
    var pool = restoreContext.pool;
    var image = restoreContext.image;
    var destPool = restorePoolEl.value.trim();
    var destImage = restoreImageEl.value.trim();
    var selectedPoint = restorePointEl.value;
    if (!restoreForm.reportValidity()) return;
    clearRestoreFeedback();
    restoreSubmit.disabled = true;
    restoreSubmit.textContent = "Đang kiểm tra preflight…";
    try {
      var response = await fetch("/backups/restore-as-new/propose", {
        method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ pool: pool, image: image, dest_pool: destPool, dest_image: destImage, recovery_point_job_id: selectedPoint })
      });
      var body = await response.json();
      if (!response.ok) {
        if (body.detail && typeof body.detail === "object" && body.detail.preflight) renderRestorePreflight(body.detail.preflight);
        throw new Error(restoreMessage(body.detail));
      }
      renderRestorePreflight(body.preflight);
      restoreSubmit.textContent = "Đã tạo đề xuất";
      window.setTimeout(function () { window.location.reload(); }, 850);
    } catch (err) {
      showRestoreError(err.message || "Không tạo được đề xuất khôi phục");
      restoreSubmit.disabled = false;
      restoreSubmit.textContent = "Kiểm tra & tạo đề xuất";
    }
  });

  var policyForm = document.getElementById("backup-policy-form");
  var policyStatus = document.getElementById("backup-policy-status");
  if (policyForm) policyForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var form = new FormData(policyForm);
    var lines = String(form.get("tracked_images") || "").split(/\r?\n/).map(function (value) { return value.trim(); }).filter(Boolean);
    var tracked = lines.map(function (value) {
      var separator = value.indexOf("/");
      return { pool: separator > 0 ? value.slice(0, separator).trim() : "", image: separator > 0 ? value.slice(separator + 1).trim() : "" };
    });
    if (tracked.some(function (item) { return !item.pool || !item.image; })) {
      if (policyStatus) policyStatus.textContent = "Mỗi dòng phải có dạng pool/image.";
      return;
    }
    if (policyStatus) policyStatus.textContent = "Đang kiểm tra và lưu…";
    fetch("/api/backups/policy", { credentials: "same-origin" })
      .then(function (response) { if (!response.ok) throw new Error("Không đọc được policy hiện tại"); return response.json(); })
      .then(function (body) {
        var policy = body.policy || {};
        var previous = {};
        (policy.tracked_images || []).forEach(function (item) { previous[item.pool + "/" + item.image] = item; });
        tracked = tracked.map(function (item) { return Object.assign({}, previous[item.pool + "/" + item.image] || {}, item); });
        policy.tracked_images = tracked;
        policy.rpo_hours = Number(form.get("rpo_hours"));
        policy.required_copy_count = Number(form.get("required_copy_count"));
        policy.retention = Object.assign({}, policy.retention || {}, {
          keep_full_count: Number(form.get("keep_full_count")),
          keep_incremental_count: Number(form.get("keep_incremental_count"))
        });
        policy.backup_targets = (policy.backup_targets || []).map(function (target) {
          return Object.assign({}, target, { immutable: form.has("target_" + target.slot + "_immutable") });
        });
        return fetch("/api/backups/policy", {
          method: "PUT", credentials: "same-origin", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ policy: policy })
        });
      })
      .then(function (response) {
        return response.json().then(function (body) { if (!response.ok) throw new Error(body.detail || "Không thể lưu policy"); return body; });
      })
      .then(function (body) {
        if (policyStatus) policyStatus.textContent = "Đã lưu revision " + String(body.revision_id || "").slice(0, 12) + ". Policy áp dụng ở chu kỳ backup kế tiếp.";
      })
      .catch(function (error) { if (policyStatus) policyStatus.textContent = error.message || "Không thể lưu policy"; });
  });
})();
