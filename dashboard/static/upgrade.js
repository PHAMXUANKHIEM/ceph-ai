(function () {
  "use strict";

  var dataEl = document.getElementById("versions-by-codename-data");
  var versionsByCodename = dataEl ? JSON.parse(dataEl.textContent || "{}") : {};

  function setHidden(element, hidden) {
    if (element) element.hidden = hidden;
  }

  function updateDaemonComparison(input) {
    var target = input ? input.value.trim() : "";
    document.querySelectorAll("#upgrade-daemon-table tbody tr[data-current-version]").forEach(function (row) {
      var current = String(row.dataset.currentVersion || "").split(",").map(function (value) { return value.trim(); });
      var scope = row.querySelector(".upgrade-daemon-scope");
      var targetWrap = row.querySelector(".upgrade-daemon-target");
      var targetValue = row.querySelector("[data-target-version]");
      row.classList.remove("is-needed", "is-ok");
      if (targetValue) targetValue.textContent = target || "—";
      if (!target) {
        if (scope) { scope.textContent = "Chưa chọn đích"; scope.className = "upgrade-daemon-scope is-dim"; }
        setHidden(targetWrap, true);
        return;
      }
      var matches = current.indexOf(target) !== -1;
      if (scope) {
        scope.textContent = matches ? "Đã đúng phiên bản" : "Cần nâng cấp";
        scope.className = "upgrade-daemon-scope " + (matches ? "is-ok" : "is-needed");
      }
      row.classList.add(matches ? "is-ok" : "is-needed");
      setHidden(targetWrap, false);
    });
  }

  function bindVersionForm(form) {
    var codename = form.querySelector(".version-picker-codename");
    var version = form.querySelector(".version-picker-version");
    var input = form.querySelector(".version-picker-input");
    if (!codename || !version || !input) return;

    function validate() {
      var valid = /^\d+\.\d+\.\d+$/.test(input.value.trim());
      input.setCustomValidity(valid ? "" : "Nhập phiên bản theo định dạng x.y.z, ví dụ 19.2.0.");
      input.classList.toggle("is-invalid", Boolean(input.value.trim()) && !valid);
      updateDaemonComparison(input);
    }

    codename.addEventListener("change", function () {
      var versions = versionsByCodename[codename.value] || [];
      version.replaceChildren();
      if (!codename.value || !versions.length) {
        version.disabled = true;
        var placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "— Chọn release trước —";
        version.appendChild(placeholder);
        validate();
        return;
      }
      version.disabled = false;
      versions.slice().reverse().forEach(function (value) {
        var option = document.createElement("option");
        option.value = value;
        option.textContent = value;
        version.appendChild(option);
      });
      input.value = versions[versions.length - 1];
      validate();
    });
    version.addEventListener("change", function () { if (version.value) { input.value = version.value; validate(); } });
    input.addEventListener("input", validate);
    validate();
  }

  document.querySelectorAll(".version-picker-form").forEach(bindVersionForm);

  var lastUpgradeTime = document.getElementById("upgrade-last-time");
  if (lastUpgradeTime && lastUpgradeTime.dateTime) {
    var upgradeDate = new Date(lastUpgradeTime.dateTime);
    if (!Number.isNaN(upgradeDate.getTime())) {
      var ageSeconds = Math.max(0, Math.floor((Date.now() - upgradeDate.getTime()) / 1000));
      var relative = ageSeconds < 60 ? "Vừa xong" : ageSeconds < 3600 ? Math.floor(ageSeconds / 60) + " phút trước" : ageSeconds < 86400 ? Math.floor(ageSeconds / 3600) + " giờ trước" : ageSeconds < 2592000 ? Math.floor(ageSeconds / 86400) + " ngày trước" : Math.floor(ageSeconds / 2592000) + " tháng trước";
      lastUpgradeTime.textContent = relative;
      lastUpgradeTime.title = upgradeDate.toLocaleString("vi-VN");
    }
  }

  function make(tag, text, className) {
    var element = document.createElement(tag);
    if (className) element.className = className;
    if (text !== undefined && text !== null) element.textContent = text;
    return element;
  }

  function parseLog(raw) {
    var lines = String(raw || "").split(/\r?\n/);
    var meta = {}, sections = {}, current = "";
    lines.forEach(function (line) {
      var heading = line.match(/^##\s+(.+)$/);
      if (heading) { current = heading[1].trim(); sections[current] = []; return; }
      var field = line.match(/^[-*]\s+\*\*(.+?):\*\*\s*(.*)$/);
      if (field && !current) meta[field[1].trim()] = field[2].trim();
      if (current) sections[current].push(line);
    });
    return {meta: meta, sections: sections};
  }

  function addLogField(parent, label, value, tag) {
    var field = make("div", undefined, "upgrade-log-field");
    field.appendChild(make("label", label));
    field.appendChild(make(tag || "span", value || "—"));
    parent.appendChild(field);
  }

  function renderStructuredLog() {
    var data = document.getElementById("upgrade-log-data");
    var root = document.getElementById("upgrade-log-structured");
    if (!data || !root) return;
    var parsed = parseLog(JSON.parse(data.textContent || '""'));
    var meta = parsed.meta, sections = parsed.sections;
    var header = make("div", undefined, "upgrade-log-header");
    addLogField(header, "Phương thức", (meta["Phương thức"] || "").replace(/\s*\(.*\)$/, ""));
    addLogField(header, "Phiên bản đích", meta["Phiên bản đích"]);
    var status = make("span", meta["Trạng thái"] || "—", "upgrade-status-badge " + (/thất bại|failed|lỗi/i.test(meta["Trạng thái"] || "") ? "is-error" : "is-success"));
    var statusField = make("div", undefined, "upgrade-log-field"); statusField.appendChild(make("label", "Trạng thái")); statusField.appendChild(status); header.appendChild(statusField);
    addLogField(header, "Đề xuất lúc", meta["Đề xuất lúc"]);
    addLogField(header, "Cập nhật", meta["Cập nhật lần cuối"]);
    root.appendChild(header);

    var summaryLines = sections["Tóm tắt cụm sau nâng cấp"] || [];
    if (summaryLines.length) {
      root.appendChild(make("h3", "Tóm tắt cụm sau nâng cấp", "upgrade-log-section-title"));
      var grid = make("div", undefined, "upgrade-log-summary-grid");
      summaryLines.forEach(function (line) {
        var field = line.match(/^[-*]\s+\*\*(.+?):\*\*\s*(.*)$/);
        if (!field) return;
        var item = make("div", undefined, "upgrade-log-summary-item"); item.appendChild(make("label", field[1])); item.appendChild(make("strong", field[2])); grid.appendChild(item);
      });
      root.appendChild(grid);
    }

    var procedure = sections["Quy trình dự kiến"];
    if (procedure) {
      var procedureDetails = make("details", undefined, "upgrade-log-detail");
      procedureDetails.appendChild(make("summary", "Quy trình dự kiến · kỹ thuật"));
      procedureDetails.appendChild(make("pre", procedure.join("\n").replace(/^```\s*|```\s*$/g, "").trim(), "upgrade-plan-text"));
      root.appendChild(procedureDetails);
    }

    var steps = sections["Các bước thực hiện theo từng node"] || [];
    var timeline = make("div", undefined, "upgrade-log-timeline");
    var stepData = [];
    steps.forEach(function (line) {
      var match = line.match(/^\s*(\d+)\.\s+\*\*(.+?)\*\*(?:\s+\((.*?)\))?\s+—\s+(.+)$/);
      if (match) { stepData.push({host: match[2], phase: match[3] || "", status: match[4], time: "", command: "", error: ""}); return; }
      var last = stepData[stepData.length - 1];
      if (!last) return;
      var time = line.match(/^\s+-\s+Thời gian:\s*(.+)$/); if (time) last.time = time[1];
      var command = line.match(/^\s+-\s+Lệnh:\s*`?(.*?)`?\s*$/); if (command) last.command = command[1];
      var error = line.match(/^\s+-\s+Lỗi:\s*`?(.*?)`?\s*$/); if (error) last.error = error[1];
    });
    if (stepData.length) {
      root.appendChild(make("h3", "Các bước thực hiện theo node", "upgrade-log-section-title"));
      stepData.forEach(function (item) {
        var failed = /lỗi|failed|thất bại/i.test(item.status) || item.error;
        var step = make("article", undefined, "upgrade-log-step" + (failed ? " is-failed" : ""));
        step.appendChild(make("span", failed ? "×" : "✓", "upgrade-log-step-icon"));
        var name = make("div"); name.appendChild(make("strong", item.host + (item.phase ? " · " + item.phase : ""))); if (item.command) name.appendChild(make("code", item.command)); step.appendChild(name);
        step.appendChild(make("span", item.status, "upgrade-log-step-status"));
        if (item.time) step.appendChild(make("span", item.time, "upgrade-log-step-time"));
        if (item.error) step.appendChild(make("div", "Lỗi: " + item.error, "upgrade-log-step-extra"));
        timeline.appendChild(step);
      });
      root.appendChild(timeline);
    }

    var failedLog = /thất bại|failed|lỗi/i.test(meta["Trạng thái"] || "") || stepData.some(function (item) { return item.error; });
    root.appendChild(make("div", failedLog ? "Có bước lỗi trong lần nâng cấp. Kiểm tra node và lệnh chi tiết trước khi thử lại." : "Change-risk analyzer: lần nâng cấp đã có bản ghi; hãy kiểm tra sức khỏe cụm trước khi thực hiện thay đổi tiếp theo.", "upgrade-log-risk" + (failedLog ? " is-error" : "")));
    var cephadmKey = Object.keys(sections).find(function (key) { return key.indexOf("Trạng thái cephadm") === 0; });
    if (cephadmKey) root.appendChild(make("div", sections[cephadmKey].join(" ").replace(/```/g, "").trim(), "upgrade-log-cephadm"));
  }

  renderStructuredLog();

  var tracker = document.getElementById("cephadm-upgrade-tracker");
  if (!tracker || !document.getElementById("upgrade-live-running")) return;
  var statusUrl = tracker.dataset.statusUrl;
  var interval = parseInt(tracker.dataset.pollInterval || "5000", 10);
  var running = document.getElementById("upgrade-live-running");
  var empty = document.getElementById("upgrade-live-empty");
  var emptyText = document.getElementById("upgrade-live-empty-text");
  var error = document.getElementById("upgrade-live-error");
  var errorText = error;
  var badge = document.querySelector(".upgrade-summary-bar .upgrade-status-badge");
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

  function setBadge(text, kind) { if (!badge) return; badge.textContent = text; badge.className = "upgrade-status-badge is-" + kind; }
  function formatProgress(status) { var value = status.progress || "—"; if (status.progress_percent !== null && status.progress_percent !== undefined) value += " (" + Math.round(status.progress_percent) + "%)"; return value; }
  function renderStatus(payload) {
    if (!payload.ok) { setHidden(error, false); errorText.textContent = payload.error || "Không rõ lỗi kết nối."; setBadge("Lỗi kết nối", "error"); return; }
    var status = payload.status || {}, action = payload.action || {}, isRunning = Boolean(status.in_progress), isPaused = Boolean(status.is_paused), failure = status.failure || status.error, actionFailed = action.status === "FAILED";
    var percent = typeof status.progress_percent === "number" && Number.isFinite(status.progress_percent) ? status.progress_percent : null;
    setHidden(error, !failure); if (failure) errorText.textContent = failure; setHidden(running, !isRunning); setHidden(empty, isRunning || Boolean(failure));
    if (isRunning) { setBadge(isPaused ? "Tạm dừng" : "Đang chạy", isPaused ? "running" : "running"); state.textContent = isPaused ? "Tạm dừng" : "Đang chạy"; target.textContent = status.target_image || "—"; progress.textContent = formatProgress(status); setHidden(progressBar, !Number.isFinite(percent)); if (Number.isFinite(percent)) { var bounded = Math.max(0, Math.min(100, percent)); progressBar.setAttribute("aria-valuenow", String(Math.round(bounded))); progressFill.style.width = bounded + "%"; } setHidden(message, !status.message); if (status.message) message.textContent = status.message; setHidden(complete, !Array.isArray(status.services_complete) || !status.services_complete.length); if (completeText && Array.isArray(status.services_complete)) completeText.textContent = status.services_complete.join(", "); setHidden(pauseForm, isPaused); setHidden(resumeForm, !isPaused); }
    else if (failure || actionFailed) { setBadge("Lỗi upgrade", "error"); if (emptyText) emptyText.textContent = "Lần nâng cấp gần nhất lỗi — xem nhật ký bên dưới."; }
    else if (action.status === "EXECUTED") { setBadge("Hoàn tất", "success"); if (emptyText) emptyText.textContent = "Không có tiến trình đang chạy"; }
    else if (action.status === "APPROVED" || action.status === "PENDING_APPROVAL") { setBadge("Đang chờ duyệt", "running"); if (emptyText) emptyText.textContent = "Đang chờ người quản trị duyệt đề xuất."; }
    else { setBadge("Sẵn sàng", "idle"); if (emptyText) emptyText.textContent = "Không có tiến trình đang chạy"; }
    if (updated) updated.textContent = "Cập nhật " + new Date().toLocaleTimeString("vi-VN");
  }
  function poll() { if (polling) return; polling = true; fetch(statusUrl, {credentials: "same-origin", cache: "no-store"}).then(function (response) { return response.json().then(function (payload) { if (!response.ok && payload.ok !== false) payload = {ok: false, error: "HTTP " + response.status}; return payload; }); }).then(renderStatus).catch(function () { renderStatus({ok: false, error: "Không gọi được API theo dõi upgrade."}); }).finally(function () { polling = false; window.setTimeout(poll, interval); }); }
  if (pauseForm) pauseForm.addEventListener("submit", function (event) { if (!window.confirm("Tạm dừng tiến trình nâng cấp? Bạn cần kiểm tra trạng thái cụm trước khi tiếp tục.")) event.preventDefault(); });
  poll();
})();
