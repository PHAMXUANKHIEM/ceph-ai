(function () {
  var catalogs = {};

  function requestCatalog(provider) {
    if (catalogs[provider]) return catalogs[provider];
    var url = provider === "codex" ? "/settings/codex/status" : "/settings/claude/status";
    catalogs[provider] = fetch(url, { credentials: "same-origin" }).then(function (response) {
      if (!response.ok) {
        return response.json().then(function (data) { throw new Error(data.detail || "HTTP " + response.status); });
      }
      return response.json();
    });
    return catalogs[provider];
  }

  function renderModels(select, status, data) {
    var current = select.value;
    var models = data && Array.isArray(data.models) ? data.models : [];
    select.innerHTML = "";
    var automatic = document.createElement("option");
    automatic.value = "";
    automatic.textContent = "Tự động (model mặc định)";
    select.appendChild(automatic);
    var found = false;
    models.forEach(function (item) {
      var id = typeof item === "string" ? item : item.id;
      if (!id) return;
      var option = document.createElement("option");
      option.value = id;
      option.textContent = typeof item === "string" ? item : (item.label || id);
      if (item && item.version) option.textContent += " " + item.version;
      if (item && item.is_default) option.textContent += " · mặc định";
      option.selected = id === current;
      found = found || id === current;
      select.appendChild(option);
    });
    if (current && !found) {
      var saved = document.createElement("option");
      saved.value = current;
      saved.textContent = current + " · đã lưu";
      saved.selected = true;
      select.appendChild(saved);
    }
    select.value = current;
    status.textContent = models.length ? "Đã tải " + models.length + " model." : "Chưa có catalog; dùng model mặc định hoặc profile riêng.";
  }

  function bindRole(providerId, modelId, statusId, sourceId, profileWrapId, profileId, customWrapId, customId) {
    var provider = document.getElementById(providerId);
    var model = document.getElementById(modelId);
    var status = document.getElementById(statusId);
    var source = document.getElementById(sourceId);
    var profileWrap = document.getElementById(profileWrapId);
    var profile = document.getElementById(profileId);
    var customWrap = document.getElementById(customWrapId);
    var custom = document.getElementById(customId);
    if (!provider || !model || !status || !source) return;

    function refreshProfile() {
      var separate = source.value === "separate";
      profileWrap.hidden = !separate;
      if (profile) profile.required = separate;
      if (separate) refreshProfileStatus();
    }
    function refreshProfileStatus() {
      if (!profile || !profile.value.trim() || provider.value === "auto") {
        if (source.value === "separate") status.textContent = provider.value === "auto"
          ? "Chọn Codex hoặc Claude để kiểm tra profile riêng."
          : "Nhập tên profile để kiểm tra trạng thái đăng nhập.";
        return;
      }
      status.textContent = "Đang kiểm tra đăng nhập profile riêng…";
      fetch("/ai-tasks/account-profile-status?provider=" + encodeURIComponent(provider.value) + "&profile=" + encodeURIComponent(profile.value.trim()), { credentials: "same-origin" })
        .then(function (response) { return response.json().then(function (data) { if (!response.ok) throw new Error(data.detail || "HTTP " + response.status); return data; }); })
        .then(function (data) {
          status.textContent = data.authenticated
            ? "✓ Profile riêng đã đăng nhập" + (data.email ? " (" + data.email + ")" : "") + "."
            : (data.installed ? "⚠ Profile riêng chưa đăng nhập." : "⚠ Chưa cài CLI " + provider.value + ".");
        })
        .catch(function (error) { status.textContent = "Không kiểm tra được profile: " + error.message; });
    }
    function refreshModels() {
      if (source.value === "separate") {
        model.innerHTML = "";
        var automatic = document.createElement("option");
        automatic.value = "";
        automatic.textContent = "Tự động (model mặc định của profile)";
        model.appendChild(automatic);
        model.value = "";
        customWrap.hidden = false;
        status.textContent = "Profile riêng không dùng catalog tài khoản mặc định; nhập model ID nếu cần.";
        return;
      }
      customWrap.hidden = true;
      if (provider.value === "auto") {
        status.textContent = "auto sẽ chọn provider khả dụng; chọn Codex hoặc Claude để xem catalog.";
        return;
      }
      status.textContent = "Đang tải catalog " + provider.value + "…";
      requestCatalog(provider.value).then(function (data) {
        renderModels(model, status, data);
      }).catch(function (error) {
        status.textContent = "Không tải được catalog: " + error.message;
      });
    }
    source.addEventListener("change", refreshProfile);
    source.addEventListener("change", refreshModels);
    provider.addEventListener("change", function () {
      refreshModels();
      if (source.value === "separate") refreshProfileStatus();
    });
    if (profile) profile.addEventListener("change", refreshProfileStatus);
    model.closest("form").addEventListener("submit", function () {
      var value = custom.value.trim();
      if (!value) return;
      var option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      option.selected = true;
      model.appendChild(option);
      model.value = value;
    });
    refreshProfile();
    refreshModels();
  }

  bindRole("task-planner-provider", "task-planner-model", "task-planner-model-status", "task-planner-account-source", "task-planner-profile-wrap", "task-planner-profile", "task-planner-custom-model-wrap", "task-planner-custom-model");
  bindRole("task-implementer-provider", "task-implementer-model", "task-implementer-model-status", "task-implementer-account-source", "task-implementer-profile-wrap", "task-implementer-profile", "task-implementer-custom-model-wrap", "task-implementer-custom-model");

  var detail = document.getElementById("ai-task-detail");
  if (!detail) return;
  var taskId = detail.getAttribute("data-task-id");
  var statusEl = document.getElementById("ai-task-status");
  var resultEl = document.getElementById("ai-task-result");
  var outputEl = document.getElementById("ai-task-test-output");
  function refreshTask() {
    fetch("/ai-tasks/" + encodeURIComponent(taskId) + "/status", { credentials: "same-origin" })
      .then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); })
      .then(function (data) {
        statusEl.textContent = data.status || "UNKNOWN";
        var parts = [];
        if (data.planner_provider || data.implementer_provider) parts.push("Planner=" + (data.planner_provider || "?") + ", Implementer=" + (data.implementer_provider || "?"));
        if (data.review_rounds) parts.push("review " + data.review_rounds + " vòng");
        if (data.branch) parts.push("branch=" + data.branch);
        if (data.commit) parts.push("commit=" + data.commit);
        if (data.changed_files && data.changed_files.length) parts.push("files=" + data.changed_files.join(", "));
        if (data.error) parts.push("Lỗi: " + data.error);
        resultEl.textContent = parts.join(" · ") || "Đang chờ worker...";
        outputEl.textContent = data.test_output || "Chưa có kết quả.";
        if (["PUSHED", "COMMITTED", "STAGING_VERIFIED", "PROMOTED", "FAILED", "FAILED_STALE", "SKIPPED_DUPLICATE"].indexOf(data.status) !== -1) return;
        window.setTimeout(refreshTask, 4000);
      })
      .catch(function (error) { resultEl.textContent = "Không đọc được trạng thái: " + error.message; window.setTimeout(refreshTask, 6000); });
  }
  refreshTask();
})();
