(function () {
  var dataEl = document.getElementById("versions-by-codename-data");
  if (dataEl) {
    var versionsByCodename = JSON.parse(dataEl.textContent || "{}");

    Array.prototype.forEach.call(document.querySelectorAll(".version-picker-form"), function (form) {
      var codenameSelect = form.querySelector(".version-picker-codename");
      var versionSelect = form.querySelector(".version-picker-version");
      var versionInput = form.querySelector(".version-picker-input");
      if (!codenameSelect || !versionSelect || !versionInput) return;

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
        versionInput.value = versions[versions.length - 1];
      });

      versionSelect.addEventListener("change", function () {
        if (versionSelect.value) versionInput.value = versionSelect.value;
      });
    });
  }

  var tracker = document.getElementById("cephadm-upgrade-tracker");
  if (!tracker || !document.getElementById("upgrade-live-running")) return;

  var statusUrl = tracker.dataset.statusUrl;
  var interval = parseInt(tracker.dataset.pollInterval || "5000", 10);
  var running = document.getElementById("upgrade-live-running");
  var empty = document.getElementById("upgrade-live-empty");
  var emptyText = document.getElementById("upgrade-live-empty-text");
  var error = document.getElementById("upgrade-live-error");
  var errorText = document.getElementById("upgrade-live-error-text");
  var badge = document.getElementById("upgrade-live-badge");
  var state = document.getElementById("upgrade-live-state");
  var target = document.getElementById("upgrade-live-target");
  var progress = document.getElementById("upgrade-live-progress");
  var progressBar = document.getElementById("upgrade-live-progress-bar");
  var progressFill = document.getElementById("upgrade-live-progress-fill");
  var message = document.getElementById("upgrade-live-message");
  var complete = document.getElementById("upgrade-live-complete");
  var completeText = complete ? complete.querySelector("span") : null;
  var updated = document.getElementById("upgrade-live-updated");
  var pauseForm = document.getElementById("upgrade-live-pause-form");
  var resumeForm = document.getElementById("upgrade-live-resume-form");
  var polling = false;

  function setHidden(element, hidden) {
    if (element) element.hidden = hidden;
  }

  function setBadge(text, kind) {
    badge.textContent = text;
    badge.className = "upgrade-live-badge is-" + kind;
  }

  function formatProgress(status) {
    var value = status.progress || "—";
    if (status.progress_percent !== null && status.progress_percent !== undefined) {
      value += " (" + Math.round(status.progress_percent) + "%)";
    }
    return value;
  }

  function renderStatus(payload) {
    if (!payload.ok) {
      setHidden(error, false);
      errorText.textContent = payload.error || "Không rõ lỗi kết nối.";
      setBadge("Mất kết nối", "error");
      if (updated) updated.textContent = "Chưa cập nhật được";
      return;
    }

    var status = payload.status || {};
    var action = payload.action || {};
    var isRunning = Boolean(status.in_progress);
    var isPaused = Boolean(status.is_paused);
    var failure = status.failure || status.error;
    var actionFailed = action.status === "FAILED";
    var percent = typeof status.progress_percent === "number" && Number.isFinite(status.progress_percent)
      ? status.progress_percent : null;

    setHidden(error, !failure);
    if (failure) errorText.textContent = failure;
    setHidden(running, !isRunning);
    setHidden(empty, isRunning || Boolean(failure));
    if (isRunning) {
      setBadge(isPaused ? "Tạm dừng" : "Đang chạy", isPaused ? "paused" : "running");
      state.textContent = isPaused ? "Tạm dừng" : "Đang chạy";
      target.textContent = status.target_image || "—";
      progress.textContent = formatProgress(status);
      setHidden(progressBar, !Number.isFinite(percent));
      if (Number.isFinite(percent)) {
        var bounded = Math.max(0, Math.min(100, percent));
        progressBar.setAttribute("aria-valuenow", String(Math.round(bounded)));
        progressFill.style.width = bounded + "%";
      }
      setHidden(message, !status.message);
      if (status.message) message.textContent = status.message;
      setHidden(complete, !Array.isArray(status.services_complete) || status.services_complete.length === 0);
      if (completeText && Array.isArray(status.services_complete)) {
        completeText.textContent = status.services_complete.join(", ");
      }
      setHidden(pauseForm, isPaused);
      setHidden(resumeForm, !isPaused);
    } else if (failure || actionFailed) {
      setBadge("Lỗi upgrade", "error");
      if (emptyText) emptyText.textContent = actionFailed
        ? "Lần nâng cấp gần nhất thất bại — xem nhật ký bên dưới."
        : "Cephadm báo lỗi — xem chi tiết bên trên.";
    } else if (action.status === "EXECUTED") {
      setBadge("Hoàn tất", "success");
      if (emptyText) emptyText.textContent = "Lần nâng cấp gần nhất đã hoàn tất.";
    } else if (action.status === "APPROVED" || action.status === "PENDING_APPROVAL") {
      setBadge("Đang chờ xử lý", "idle");
      if (emptyText) emptyText.textContent = action.status === "APPROVED"
        ? "Đã duyệt — đang chờ Worker gửi lệnh vào cụm."
        : "Đang chờ người quản trị duyệt đề xuất.";
    } else {
      setBadge("Sẵn sàng", "idle");
      if (emptyText) emptyText.textContent = "Không có tiến trình nâng cấp nào đang chạy trên cụm hiện tại.";
    }
    if (updated) updated.textContent = "Vừa cập nhật — " + new Date().toLocaleTimeString("vi-VN");
  }

  function poll() {
    if (polling) return;
    polling = true;
    fetch(statusUrl, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        return response.json().then(function (payload) {
          if (!response.ok && payload.ok !== false) payload = { ok: false, error: "HTTP " + response.status };
          return payload;
        });
      })
      .then(renderStatus)
      .catch(function () { renderStatus({ ok: false, error: "Không gọi được API theo dõi upgrade." }); })
      .finally(function () {
        polling = false;
        window.setTimeout(poll, interval);
      });
  }

  poll();
})();
