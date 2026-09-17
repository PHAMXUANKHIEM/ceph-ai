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
    if (!window.confirm("Đề xuất Settings sẽ tạo một hành động áp dụng lên các node Ceph thật. Bạn có muốn tiếp tục không?")) event.preventDefault();
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
