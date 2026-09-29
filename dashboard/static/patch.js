(function () {
  "use strict";

  var input = document.getElementById("patch-file-input");
  var zone = document.getElementById("patch-dropzone");
  var name = document.getElementById("patch-file-name");
  var size = document.getElementById("patch-file-size");
  var error = document.getElementById("patch-upload-error");
  if (input && zone) {
    function formatSize(bytes) {
      if (bytes < 1024) return bytes + " B";
      if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
      return (bytes / (1024 * 1024)).toFixed(2) + " MB";
    }
    function choose(file) {
      if (!file) return;
      var lower = file.name.toLowerCase();
      if (!lower.endsWith(".patch") && !lower.endsWith(".diff")) {
        error.textContent = "Chỉ chấp nhận file .patch hoặc .diff.";
        error.hidden = false;
        input.value = "";
        return;
      }
      if (file.size > 2 * 1024 * 1024) {
        error.textContent = "File quá lớn. Giới hạn là 2 MB.";
        error.hidden = false;
        input.value = "";
        return;
      }
      error.hidden = true;
      name.textContent = file.name;
      name.classList.add("is-selected");
      size.textContent = formatSize(file.size);
    }
    input.addEventListener("change", function () { choose(input.files[0]); });
    ["dragenter", "dragover"].forEach(function (eventName) { zone.addEventListener(eventName, function (event) { event.preventDefault(); zone.classList.add("is-dragover"); }); });
    ["dragleave", "drop"].forEach(function (eventName) { zone.addEventListener(eventName, function (event) { event.preventDefault(); zone.classList.remove("is-dragover"); }); });
    zone.addEventListener("drop", function (event) {
      var file = event.dataTransfer.files[0];
      if (!file) return;
      try {
        var transfer = new DataTransfer();
        transfer.items.add(file);
        input.files = transfer.files;
      } catch (ignore) { /* The file is still shown; old browsers can click to select. */ }
      choose(file);
    });
    zone.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); input.click(); } });
  }

  var installForm = document.getElementById("patch-install-form");
  if (installForm) installForm.addEventListener("submit", function (event) {
    event.preventDefault();
    if (!window.confirm("Đề xuất Settings sẽ tạo một hành động áp dụng lên các node Ceph thật. Bạn có muốn tiếp tục không?")) return;

    var button = document.getElementById("patch-install-button");
    var actionRow = installForm.closest(".patch-settings-action");
    var settingsSection = installForm.closest(".patch-settings-step");
    var statusText = actionRow && actionRow.querySelector("strong");
    var timeText = actionRow && actionRow.querySelector("span");
    var errorBox = document.getElementById("patch-install-error");
    if (button) { button.disabled = true; button.textContent = "Đang tạo đề xuất…"; }
    if (errorBox) { errorBox.hidden = true; errorBox.textContent = ""; }

    fetch(installForm.action, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Accept": "application/json" }
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.detail || "HTTP " + response.status);
        return data;
      });
    }).then(function (data) {
      if (statusText) statusText.textContent = "Lần gần nhất: PENDING APPROVAL";
      if (timeText) timeText.textContent = "Đề xuất vừa được tạo";
      if (button) button.textContent = "Đang chờ duyệt";

      var card = document.createElement("article");
      card.className = "patch-approval-card";
      var heading = document.createElement("div");
      var kicker = document.createElement("span");
      kicker.className = "patch-kicker";
      kicker.textContent = "ACTION REQUIRES APPROVAL";
      var title = document.createElement("h3");
      title.textContent = "Đề xuất Settings đang chờ duyệt";
      var status = document.createElement("p");
      status.textContent = String(data.status || "PENDING_APPROVAL").replace(/_/g, " ");
      heading.appendChild(kicker);
      heading.appendChild(title);
      heading.appendChild(status);

      var actions = document.createElement("div");
      actions.className = "pending-action-buttons";
      [["approve", "Duyệt", "btn btn-approve"], ["reject", "Từ chối", "btn btn-reject"]].forEach(function (item) {
        var form = document.createElement("form");
        form.method = "post";
        form.action = "/actions/" + encodeURIComponent(data.action_id) + "/" + item[0];
        form.className = "inline-form";
        var submit = document.createElement("button");
        submit.type = "submit";
        submit.className = item[2];
        submit.textContent = item[1];
        form.appendChild(submit);
        actions.appendChild(form);
      });

      var details = document.createElement("details");
      var summary = document.createElement("summary");
      summary.textContent = "Xem kế hoạch";
      var plan = document.createElement("pre");
      plan.className = "upgrade-plan-text";
      plan.textContent = data.rationale || "Không có nội dung kế hoạch.";
      details.appendChild(summary);
      details.appendChild(plan);
      card.appendChild(heading);
      card.appendChild(actions);
      card.appendChild(details);
      if (settingsSection) settingsSection.insertBefore(card, actionRow);
    }).catch(function (err) {
      if (button) { button.disabled = false; button.textContent = "Đề xuất Settings"; }
      if (errorBox) { errorBox.textContent = err.message || "Không tạo được đề xuất Settings."; errorBox.hidden = false; }
    });
  });

  var progress = document.getElementById("patch-build-progress");
  if (!progress) return;
  var title = document.getElementById("patch-progress-title");
  var phase = document.getElementById("patch-progress-phase");
  var elapsed = document.getElementById("patch-progress-elapsed");
  var log = document.getElementById("patch-progress-log");
  var fill = document.getElementById("patch-progress-fill");
  var live = document.getElementById("patch-progress-live");
  var activeStatuses = ["PENDING_APPROVAL", "APPROVED", "EXECUTING", "GRACE_PENDING"];
  function statusText(status) {
    return {PENDING_APPROVAL: "Chờ duyệt", APPROVED: "Đã duyệt — chờ worker", EXECUTING: "Đang Build & Copy…", GRACE_PENDING: "Đang chờ xác nhận", EXECUTED: "Hoàn tất ✓", FAILED: "Thất bại", REJECTED: "Đã từ chối"}[status] || status || "Chưa chạy Build & Copy";
  }
  function render(data) {
    var status = data.status || "";
    title.textContent = statusText(status);
    live.textContent = activeStatuses.indexOf(status) >= 0 ? "LIVE" : "";
    live.hidden = activeStatuses.indexOf(status) < 0;
    var entries = Array.isArray(data.progress) ? data.progress : [];
    var done = entries.filter(function (item) { return ["done", "completed", "success"].indexOf(item.status) >= 0; }).length;
    var percent = status === "EXECUTED" ? 100 : entries.length ? Math.round(done / entries.length * 100) : (status === "EXECUTING" ? 12 : 0);
    fill.style.width = percent + "%";
    progress.querySelector("[role=progressbar]").setAttribute("aria-valuenow", String(percent));
    phase.textContent = status === "EXECUTING" ? (entries.some(function (item) { return item.status === "running"; }) ? "Đang chạy trên node…" : "Đang xử lý trên build server…") : statusText(status);
    if (entries.length) log.textContent = entries.map(function (item) { return (item.host || item.step || "worker") + " · " + (item.status || "") + (item.error ? " · " + item.error : ""); }).join("\n");
    if (data.updated_at) elapsed.textContent = "Cập nhật " + data.updated_at;
    log.scrollTop = log.scrollHeight;
  }
  function poll() {
    fetch("/patch/progress", {headers: {"Accept": "application/json", "Cache-Control": "no-cache"}}).then(function (response) { if (!response.ok) throw new Error("progress request failed"); return response.json(); }).then(function (data) {
      render(data);
      if (activeStatuses.indexOf(data.status) >= 0) window.setTimeout(poll, 2000);
    }).catch(function () { window.setTimeout(poll, 5000); });
  }
  if (activeStatuses.indexOf(progress.dataset.status) >= 0) poll();
})();
