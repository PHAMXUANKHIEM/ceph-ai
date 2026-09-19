(function () {
  var codexLoginBtn = document.getElementById("codex-login-btn");
  var codexLogoutBtn = document.getElementById("codex-logout-btn");
  var codexStatus = document.getElementById("codex-account-status");
  var codexFlow = document.getElementById("codex-device-flow");
  var codexLink = document.getElementById("codex-verification-link");
  var codexCode = document.getElementById("codex-user-code");
  var codexInstallPrompt = document.getElementById("codex-install-prompt");
  var codexInstallYesBtn = document.getElementById("codex-install-yes-btn");
  var codexInstallNoBtn = document.getElementById("codex-install-no-btn");
  var codexModelPanel = document.getElementById("codex-model-panel");
  var codexModelSelect = document.getElementById("codex-model-select");
  var codexModelSaveBtn = document.getElementById("codex-model-save-btn");
  var codexModelResult = document.getElementById("codex-model-result");
  var codexLimitPanel = document.getElementById("codex-limit-panel");
  var codexPollTimer = null;

  function codexRequest(url, options) {
    return fetch(url, Object.assign({ credentials: "same-origin" }, options || {})).then(function (response) {
      if (!response.ok) return response.json().then(function (data) { throw new Error(data.detail || "HTTP " + response.status); });
      return response.json();
    });
  }

  function renderCodexStatus(data) {
    if (!codexStatus) return;
    window.dispatchEvent(new CustomEvent("ceph-ai-account-state", { detail: { provider: "openai", enabled: !!data.enabled, configured: !!data.authenticated || !!data.enabled } }));
    if (data.installed === false) {
      codexStatus.textContent = "⚠️ Chưa cài Codex CLI trên server.";
      if (codexInstallPrompt) codexInstallPrompt.hidden = false;
      if (codexLoginBtn) codexLoginBtn.hidden = true;
      if (codexLogoutBtn) codexLogoutBtn.hidden = true;
      if (codexModelPanel) codexModelPanel.hidden = true;
      if (codexLimitPanel) codexLimitPanel.hidden = true;
    } else if (data.authenticated) {
      if (codexInstallPrompt) codexInstallPrompt.hidden = true;
      var detail = data.email || "tài khoản ChatGPT";
      if (data.plan_type) detail += " · gói " + data.plan_type;
      codexStatus.textContent = "✅ Đã đăng nhập " + detail + (data.enabled ? " — đang dùng cho Chat-with-AI" : "");
      if (codexLoginBtn) codexLoginBtn.hidden = true;
      if (codexLogoutBtn) codexLogoutBtn.hidden = false;
      renderAccountModels(codexModelPanel, codexModelSelect, data.models || [], data.model, true);
      renderAiLimits(codexLimitPanel, data.limits || []);
      if (codexFlow) codexFlow.hidden = true;
    } else {
      if (codexInstallPrompt) codexInstallPrompt.hidden = true;
      codexStatus.textContent = data.error ? "❌ " + data.error : "Chưa đăng nhập tài khoản Codex.";
      if (codexLoginBtn) codexLoginBtn.hidden = false;
      if (codexLogoutBtn) codexLogoutBtn.hidden = true;
      if (codexModelPanel) codexModelPanel.hidden = true;
      if (codexLimitPanel) codexLimitPanel.hidden = true;
    }
  }

  function renderAccountModels(panel, select, models, selected, includeAutomatic) {
    if (!panel || !select) return;
    select.innerHTML = "";
    if (includeAutomatic) {
      var automatic = document.createElement("option");
      automatic.value = "";
      automatic.textContent = "Tự động (model mặc định của tài khoản)";
      select.appendChild(automatic);
    }
    models.forEach(function (model) {
      var option = document.createElement("option");
      option.value = model.id;
      option.textContent = model.label + (model.version ? " " + model.version : "") + (model.is_default ? " · mặc định" : "");
      select.appendChild(option);
    });
    select.value = selected || (includeAutomatic ? "" : "default");
    panel.hidden = false;
  }

  function renderAiLimits(panel, limits) {
    if (!panel) return;
    panel.innerHTML = "";
    var title = document.createElement("strong");
    title.textContent = "Hạn mức AI";
    panel.appendChild(title);
    if (!limits.length) {
      var unavailable = document.createElement("p");
      unavailable.className = "hint";
      unavailable.textContent = "Nhà cung cấp chưa trả dữ liệu hạn mức cho tài khoản này.";
      panel.appendChild(unavailable);
    }
    limits.forEach(function (limit) {
      var remaining = Math.max(0, Math.min(100, Number(limit.remaining_percent) || 0));
      var used = limit.used_percent == null ? NaN : Number(limit.used_percent);
      if (!Number.isFinite(used)) used = 100 - remaining;
      used = Math.max(0, Math.min(100, used));
      var severity = used < 50 ? "ok" : (used <= 80 ? "warning" : "danger");
      var row = document.createElement("div");
      row.className = "ai-limit-row ai-limit-" + severity;
      row.title = limit.resets_at ? "Reset: " + limit.resets_at : "Thời gian reset chưa được provider cung cấp";
      var label = document.createElement("span");
      label.textContent = limit.label;
      var value = document.createElement("strong");
      value.textContent = remaining + "% còn · đã dùng " + used + "%";
      var meter = document.createElement("div");
      meter.className = "ai-limit-meter";
      var fill = document.createElement("span");
      fill.style.width = used + "%";
      meter.appendChild(fill);
      row.appendChild(label); row.appendChild(meter); row.appendChild(value);
      panel.appendChild(row);
    });
    panel.hidden = false;
  }

  function saveAccountModel(provider, select, button, result) {
    if (!select || !button) return;
    button.disabled = true;
    if (result) { result.hidden = false; result.textContent = "Đang lưu..."; }
    var body = new URLSearchParams();
    body.set("model", select.value);
    codexRequest("/settings/" + provider + "/model", { method: "POST", body: body }).then(function () {
      if (result) { result.className = "ai-test-result ai-test-ok"; result.textContent = "✅ Đã lưu"; }
    }).catch(function (err) {
      if (result) { result.className = "ai-test-result ai-test-fail"; result.textContent = "❌ " + err.message; }
    }).then(function () { button.disabled = false; });
  }

  function refreshCodexStatus(activate) {
    if (!codexStatus) return Promise.resolve();
    return codexRequest("/settings/codex/status").then(function (data) {
      renderCodexStatus(data);
      if (activate && data.authenticated && !data.enabled) {
        return codexRequest("/settings/codex/activate", { method: "POST" }).then(function () {
          data.enabled = true;
          renderCodexStatus(data);
          clearInterval(codexPollTimer);
        });
      }
    });
  }

  if (codexStatus) refreshCodexStatus(false);
  if (codexInstallNoBtn) codexInstallNoBtn.addEventListener("click", function () {
    codexInstallPrompt.hidden = true;
    codexStatus.textContent = "Chưa cài Codex CLI. Bạn có thể cài sau bằng cách tải lại trang.";
  });
  if (codexInstallYesBtn) codexInstallYesBtn.addEventListener("click", function () {
    codexInstallYesBtn.disabled = true;
    codexInstallNoBtn.disabled = true;
    codexStatus.textContent = "Đang tải và cài Codex CLI từ OpenAI...";
    codexRequest("/settings/codex/install", { method: "POST" }).then(function () {
      codexStatus.textContent = "✅ Đã cài Codex CLI. Đang kiểm tra...";
      return refreshCodexStatus(false);
    }).catch(function (err) {
      codexStatus.textContent = "❌ " + err.message;
      codexInstallYesBtn.disabled = false;
      codexInstallNoBtn.disabled = false;
    });
  });
  if (codexLoginBtn) codexLoginBtn.addEventListener("click", function () {
    codexLoginBtn.disabled = true;
    codexStatus.textContent = "Đang tạo mã đăng nhập...";
    codexRequest("/settings/codex/login/start", { method: "POST" }).then(function (data) {
      codexLink.href = data.verification_url;
      codexCode.textContent = data.user_code;
      codexFlow.hidden = false;
      codexStatus.textContent = "Đang chờ bạn hoàn tất đăng nhập...";
      window.open(data.verification_url, "_blank", "noopener");
      codexPollTimer = setInterval(function () { refreshCodexStatus(true); }, 2500);
    }).catch(function (err) {
      codexStatus.textContent = "❌ " + err.message;
      codexLoginBtn.disabled = false;
    });
  });
  if (codexLogoutBtn) codexLogoutBtn.addEventListener("click", function () {
    codexRequest("/settings/codex/logout", { method: "POST" }).then(function () {
      renderCodexStatus({ authenticated: false, enabled: false });
      window.dispatchEvent(new CustomEvent("ceph-ai-account-state", { detail: { provider: "openai", enabled: false, configured: true, loggedOut: true } }));
    }).catch(function (err) { codexStatus.textContent = "❌ " + err.message; });
  });
  if (codexModelSaveBtn) codexModelSaveBtn.addEventListener("click", function () {
    saveAccountModel("codex", codexModelSelect, codexModelSaveBtn, codexModelResult);
  });

  var claudeLoginBtn = document.getElementById("claude-login-btn");
  var claudeLogoutBtn = document.getElementById("claude-logout-btn");
  var claudeStatus = document.getElementById("claude-account-status");
  var claudeFlow = document.getElementById("claude-device-flow");
  var claudeLink = document.getElementById("claude-verification-link");
  var claudeAuthenticationCode = document.getElementById("claude-authentication-code");
  var claudeConnectBtn = document.getElementById("claude-connect-btn");
  var claudeInstallPrompt = document.getElementById("claude-install-prompt");
  var claudeInstallYesBtn = document.getElementById("claude-install-yes-btn");
  var claudeInstallNoBtn = document.getElementById("claude-install-no-btn");
  var claudeModelPanel = document.getElementById("claude-model-panel");
  var claudeModelSelect = document.getElementById("claude-model-select");
  var claudeEffortSelect = document.getElementById("claude-effort-select");
  var claudeModelSaveBtn = document.getElementById("claude-model-save-btn");
  var claudeModelResult = document.getElementById("claude-model-result");
  var claudeLimitPanel = document.getElementById("claude-limit-panel");
  var claudePollTimer = null;

  function renderClaudeStatus(data) {
    if (!claudeStatus) return;
    window.dispatchEvent(new CustomEvent("ceph-ai-account-state", { detail: { provider: "anthropic", enabled: !!data.enabled, configured: !!data.authenticated || !!data.enabled } }));
    if (data.installed === false) {
      claudeStatus.textContent = "⚠️ Chưa cài Claude Code CLI trên server.";
      if (claudeInstallPrompt) claudeInstallPrompt.hidden = false;
      if (claudeLoginBtn) claudeLoginBtn.hidden = true;
      if (claudeLogoutBtn) claudeLogoutBtn.hidden = true;
      if (claudeModelPanel) claudeModelPanel.hidden = true;
      if (claudeLimitPanel) claudeLimitPanel.hidden = true;
    } else if (data.authenticated) {
      if (claudeInstallPrompt) claudeInstallPrompt.hidden = true;
      var detail = data.email || data.auth_method || "tài khoản Claude";
      claudeStatus.textContent = "✅ Đã đăng nhập " + detail + (data.enabled ? " — đang dùng cho Chat và phân tích hiệu năng" : "");
      if (claudeLoginBtn) claudeLoginBtn.hidden = true;
      if (claudeLogoutBtn) claudeLogoutBtn.hidden = false;
      renderAccountModels(claudeModelPanel, claudeModelSelect, data.models || [], data.model, false);
      renderAccountModels(claudeModelPanel, claudeEffortSelect, data.efforts || [], data.effort, false);
      renderAiLimits(claudeLimitPanel, data.limits || []);
      if (claudeFlow) claudeFlow.hidden = true;
      if (claudeAuthenticationCode) claudeAuthenticationCode.value = "";
    } else {
      if (claudeInstallPrompt) claudeInstallPrompt.hidden = true;
      claudeStatus.textContent = data.error ? "❌ " + data.error : "Chưa đăng nhập tài khoản Claude.";
      if (claudeLoginBtn) { claudeLoginBtn.hidden = false; claudeLoginBtn.disabled = false; }
      if (claudeLogoutBtn) claudeLogoutBtn.hidden = true;
      if (claudeModelPanel) claudeModelPanel.hidden = true;
      if (claudeLimitPanel) claudeLimitPanel.hidden = true;
    }
  }

  function refreshClaudeStatus(activate) {
    if (!claudeStatus) return Promise.resolve();
    return codexRequest("/settings/claude/status").then(function (data) {
      renderClaudeStatus(data);
      if (activate && data.authenticated && !data.enabled) {
        return codexRequest("/settings/claude/activate", { method: "POST" }).then(function () {
          data.enabled = true;
          renderClaudeStatus(data);
          clearInterval(claudePollTimer);
          refreshCodexStatus(false);
        });
      }
    });
  }

  if (claudeStatus) refreshClaudeStatus(false);
  if (claudeInstallNoBtn) claudeInstallNoBtn.addEventListener("click", function () {
    claudeInstallPrompt.hidden = true;
    claudeStatus.textContent = "Chưa cài Claude Code. Bạn có thể cài sau bằng cách tải lại trang.";
  });
  if (claudeInstallYesBtn) claudeInstallYesBtn.addEventListener("click", function () {
    claudeInstallYesBtn.disabled = true;
    claudeInstallNoBtn.disabled = true;
    claudeStatus.textContent = "Đang tải và cài Claude Code từ Anthropic...";
    codexRequest("/settings/claude/install", { method: "POST" }).then(function () {
      return refreshClaudeStatus(false);
    }).catch(function (err) {
      claudeStatus.textContent = "❌ " + err.message;
      claudeInstallYesBtn.disabled = false;
      claudeInstallNoBtn.disabled = false;
    });
  });
  if (claudeLoginBtn) claudeLoginBtn.addEventListener("click", function () {
    claudeLoginBtn.disabled = true;
    claudeStatus.textContent = "Đang tạo phiên đăng nhập Claude...";
    codexRequest("/settings/claude/login/start", { method: "POST" }).then(function (data) {
      claudeLink.href = data.verification_url;
      claudeFlow.hidden = false;
      claudeStatus.textContent = "Đang chờ Authentication code từ trang Claude...";
      window.open(data.verification_url, "_blank", "noopener");
    }).catch(function (err) {
      claudeStatus.textContent = "❌ " + err.message;
      claudeLoginBtn.disabled = false;
    });
  });
  if (claudeConnectBtn) claudeConnectBtn.addEventListener("click", function () {
    var authenticationCode = (claudeAuthenticationCode.value || "").trim();
    if (!authenticationCode) {
      claudeStatus.textContent = "❌ Vui lòng nhập Authentication code.";
      claudeAuthenticationCode.focus();
      return;
    }
    claudeConnectBtn.disabled = true;
    claudeAuthenticationCode.disabled = true;
    claudeStatus.textContent = "Đang gửi mã và kết nối tới Claude...";
    var body = new URLSearchParams();
    body.set("authentication_code", authenticationCode);
    codexRequest("/settings/claude/login/complete", { method: "POST", body: body }).then(function () {
      return codexRequest("/settings/claude/activate", { method: "POST" });
    }).then(function () {
      return refreshClaudeStatus(false);
    }).then(function () {
      refreshCodexStatus(false);
    }).catch(function (err) {
      claudeStatus.textContent = "❌ " + err.message;
      claudeConnectBtn.disabled = false;
      claudeAuthenticationCode.disabled = false;
      claudeAuthenticationCode.focus();
    });
  });
  if (claudeLogoutBtn) claudeLogoutBtn.addEventListener("click", function () {
    codexRequest("/settings/claude/logout", { method: "POST" }).then(function () {
      renderClaudeStatus({ installed: true, authenticated: false, enabled: false });
      window.dispatchEvent(new CustomEvent("ceph-ai-account-state", { detail: { provider: "anthropic", enabled: false, configured: true, loggedOut: true } }));
    }).catch(function (err) { claudeStatus.textContent = "❌ " + err.message; });
  });
  if (claudeModelSaveBtn) claudeModelSaveBtn.addEventListener("click", function () {
    if (!claudeModelSelect || !claudeEffortSelect) return;
    claudeModelSaveBtn.disabled = true;
    if (claudeModelResult) { claudeModelResult.hidden = false; claudeModelResult.textContent = "Đang lưu..."; }
    var body = new URLSearchParams();
    body.set("model", claudeModelSelect.value);
    body.set("effort", claudeEffortSelect.value);
    codexRequest("/settings/claude/model", { method: "POST", body: body }).then(function () {
      if (claudeModelResult) { claudeModelResult.className = "ai-test-result ai-test-ok"; claudeModelResult.textContent = "✅ Đã lưu"; }
    }).catch(function (err) {
      if (claudeModelResult) { claudeModelResult.className = "ai-test-result ai-test-fail"; claudeModelResult.textContent = "❌ " + err.message; }
    }).then(function () { claudeModelSaveBtn.disabled = false; });
  });

  var verifyBtn = document.getElementById("router-verify-btn");
  if (!verifyBtn) {
    return; // not on the Settings page
  }

  var apiKeyInput = document.getElementById("router-api-key-input");
  var baseUrlInput = document.getElementById("router-base-url-input");
  var resultEl = document.getElementById("router-verify-result");
  var step2Form = document.getElementById("router-step2-form");
  var step2BaseUrl = document.getElementById("router-step2-base-url");
  var step2ApiKey = document.getElementById("router-step2-api-key");
  var step2Provider = document.getElementById("router-step2-provider");
  var modelSelect = document.getElementById("router-model-select");
  var modelFilter = document.getElementById("router-model-filter");
  var connectedView = document.getElementById("router-connected-view");
  var wizardView = document.getElementById("router-wizard-view");
  var changeModelBtn = document.getElementById("router-change-model-btn");
  var providerInputs = Array.prototype.slice.call(
    document.querySelectorAll('input[name="router_provider"]')
  );

  function selectedProvider() {
    var checked = providerInputs.filter(function (r) { return r.checked; })[0];
    return checked || providerInputs[0];
  }

  function selectedProviderLabel() {
    var checked = selectedProvider();
    return checked ? checked.parentNode.textContent.trim() : "API AI";
  }

  // Claude/Codex/OpenRouter each have one fixed, well-known Base URL — pick
  // it for the operator on selection so they never have to look it up.
  // 9router has none (data-base-url=""): it's always a self-hosted
  // host:port, so switching TO it leaves whatever the operator already
  // typed untouched instead of clobbering it with a guess.
  providerInputs.forEach(function (input) {
    input.addEventListener("change", function () {
      var preset = input.getAttribute("data-base-url");
      if (preset) {
        baseUrlInput.value = preset;
      }
      baseUrlInput.placeholder = input.getAttribute("data-base-url-placeholder") || "";
      resultEl.hidden = true;
      step2Form.hidden = true;
    });
  });

  // Model count above which the plain <select> gets a search/filter box in
  // front of it — spec: "sorted alphabetically, shown as dropdown/radio
  // with search/filter if >10 items".
  var FILTER_THRESHOLD = 10;

  function handleAuthRedirect(response) {
    if (response.redirected && response.url.indexOf("/login") !== -1) {
      window.location.reload();
      throw new Error("unauthenticated");
    }
    return response;
  }

  function showResult(ok, text) {
    resultEl.hidden = false;
    resultEl.classList.toggle("ai-test-ok", ok);
    resultEl.classList.toggle("ai-test-fail", !ok);
    resultEl.textContent = (ok ? "✅ " : "❌ ") + text;
  }

  function renderModelOptions(models) {
    var sorted = models.slice().sort();
    while (modelSelect.firstChild) modelSelect.removeChild(modelSelect.firstChild);
    sorted.forEach(function (id) {
      var option = document.createElement("option");
      option.value = id;
      option.textContent = id;
      modelSelect.appendChild(option);
    });
    modelFilter.hidden = sorted.length <= FILTER_THRESHOLD;
    modelFilter.value = "";
  }

  modelFilter.addEventListener("input", function () {
    var needle = modelFilter.value.trim().toLowerCase();
    Array.prototype.forEach.call(modelSelect.options, function (option) {
      option.hidden = needle !== "" && option.value.toLowerCase().indexOf(needle) === -1;
    });
  });

  verifyBtn.addEventListener("click", function () {
    // A blank api key/base_url is fine here — the server falls back to
    // whatever is already saved (see settings_verify_router in
    // dashboard/routes/settings.py), since the key field never gets
    // pre-filled with the real, already-saved value.
    var apiKey = apiKeyInput.value.trim();
    var baseUrl = baseUrlInput.value.trim();
    var provider = selectedProvider();
    var providerValue = provider ? provider.value : "9router";
    var providerLabel = selectedProviderLabel();

    verifyBtn.disabled = true;
    resultEl.hidden = true;
    resultEl.classList.remove("ai-test-ok", "ai-test-fail");
    resultEl.hidden = false;
    resultEl.textContent = "Đang kết nối " + providerLabel + "...";

    var body = new URLSearchParams();
    body.set("router_api_key", apiKey);
    body.set("router_base_url", baseUrl);
    body.set("router_provider", providerValue);

    fetch("/settings/9router/verify", { method: "POST", credentials: "same-origin", body: body })
      .then(handleAuthRedirect)
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (data) {
            throw new Error(data.detail || "HTTP " + response.status);
          });
        }
        return response.json();
      })
      .then(function (data) {
        if (!data.valid) {
          showResult(false, data.message || "Kết nối thất bại");
          step2Form.hidden = true;
          return;
        }
        step2BaseUrl.value = baseUrl;
        step2ApiKey.value = apiKey;
        step2Provider.value = providerValue;
        if (data.models && data.models.length) {
          renderModelOptions(data.models);
          showResult(true, data.message || "Kết nối thành công — tìm thấy " + data.models.length + " model");
          step2Form.hidden = false;
        } else {
          showResult(true, (data.message || "Kết nối thành công") + " (không lấy được danh sách model).");
          step2Form.hidden = true;
        }
      })
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
        showResult(false, "Không thể kết nối " + (baseUrl || providerLabel) + " — kiểm tra host/port");
      })
      .finally(function () {
        verifyBtn.disabled = false;
      });
  });

  if (changeModelBtn) {
    changeModelBtn.addEventListener("click", function () {
      connectedView.hidden = true;
      wizardView.hidden = false;
      apiKeyInput.value = "";
      resultEl.hidden = true;
      step2Form.hidden = true;
    });
  }

  // Kết nối Database: side-effect-free probe plus UI-only controls for the
  // Database panel. The Settings sidebar/navigation above remains untouched.
  var dbTestBtn = document.getElementById("db-test-btn");
  if (dbTestBtn) {
    var dbHostInput = document.getElementById("db-host-input");
    var dbPortInput = document.getElementById("db-port-input");
    var dbNameInput = document.getElementById("db-name-input");
    var dbUsernameInput = document.getElementById("db-username-input");
    var dbPasswordInput = document.getElementById("db-password-input");
    var dbUrlInput = document.getElementById("db-url-input");
    var dbSslModeInput = document.getElementById("db-ssl-mode-input");
    var dbSslLabel = document.getElementById("db-ssl-label");
    var dbSslMenu = document.getElementById("db-ssl-menu");
    var dbSslTrigger = document.querySelector("#db-ssl-select .db-select-trigger");
    var dbTimeoutInput = document.getElementById("db-connect-timeout");
    var dbPasswordToggle = document.getElementById("db-password-toggle");
    var dbResultEl = document.getElementById("db-test-result");
    var dbStatusChip = document.getElementById("db-status-chip");
    var dbStatusText = document.getElementById("db-status-text");
    var dbStatusMeta = document.getElementById("db-status-meta");
    var dbCopyBtn = document.getElementById("db-copy-btn");

    // Two input modes for the same underlying DATABASE_URL. Disable hidden
    // controls so required fields in the inactive mode cannot block submit.
    var dbModeRadios = Array.prototype.slice.call(document.querySelectorAll('input[name="db_input_mode"]'));
    var dbModeFields = Array.prototype.slice.call(document.querySelectorAll("[data-db-modes]"));
    if (dbModeRadios.length) {
      var applyDbModeVisibility = function () {
        var checked = dbModeRadios.filter(function (r) { return r.checked; })[0];
        var mode = checked ? checked.value : "fields";
        dbModeFields.forEach(function (field) {
          field.hidden = field.getAttribute("data-db-modes") !== mode;
          field.querySelectorAll("input, textarea, select").forEach(function (control) { control.disabled = field.hidden; });
        });
      };
      dbModeRadios.forEach(function (r) { r.addEventListener("change", applyDbModeVisibility); });
      applyDbModeVisibility();
    }

    if (dbSslTrigger && dbSslMenu && dbSslModeInput) {
      dbSslTrigger.addEventListener("click", function () { var open = dbSslMenu.hidden; dbSslMenu.hidden = !open; dbSslTrigger.setAttribute("aria-expanded", open ? "true" : "false"); });
      dbSslMenu.querySelectorAll("[data-ssl-mode]").forEach(function (option) { option.addEventListener("click", function () { dbSslModeInput.value = option.getAttribute("data-ssl-mode"); dbSslLabel.textContent = dbSslModeInput.value; dbSslMenu.hidden = true; dbSslTrigger.setAttribute("aria-expanded", "false"); }); });
      document.addEventListener("click", function (event) { if (!event.target.closest("#db-ssl-select")) { dbSslMenu.hidden = true; dbSslTrigger.setAttribute("aria-expanded", "false"); } });
    }
    if (dbPasswordToggle && dbPasswordInput) dbPasswordToggle.addEventListener("click", function () { var hidden = dbPasswordInput.type === "password"; dbPasswordInput.type = hidden ? "text" : "password"; dbPasswordToggle.setAttribute("aria-label", hidden ? "Ẩn mật khẩu" : "Hiện mật khẩu"); });
    if (dbCopyBtn) dbCopyBtn.addEventListener("click", function () { var value = document.getElementById("db-connection-value").textContent.trim(); var copied = function () { dbCopyBtn.classList.add("is-copied"); dbCopyBtn.textContent = "✓ Đã copy!"; setTimeout(function () { dbCopyBtn.classList.remove("is-copied"); dbCopyBtn.textContent = "▣ Copy"; }, 2000); }; if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(value).then(copied).catch(function () { fallback(value); }); else fallback(value); function fallback(text) { var area = document.createElement("textarea"); area.value = text; document.body.appendChild(area); area.select(); try { document.execCommand("copy"); copied(); } finally { area.remove(); } } });

    function refreshDatabaseStatus() {
      if (!dbStatusChip) return;
      fetch("/api/settings/database/status", { credentials: "same-origin" }).then(handleAuthRedirect).then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); }).then(function (data) { var ok = !!data.connected; dbStatusChip.className = "db-status-chip " + (ok ? "is-ok" : "is-error"); dbStatusText.textContent = ok ? "Đang kết nối" : "Mất kết nối"; dbStatusMeta.textContent = ok && data.latency_ms != null ? "~" + data.latency_ms + "ms" : ""; }).catch(function () { dbStatusChip.className = "db-status-chip is-error"; dbStatusText.textContent = "Mất kết nối"; dbStatusMeta.textContent = "Không kiểm tra được"; });
    }
    refreshDatabaseStatus();
    setInterval(refreshDatabaseStatus, 30000);

    dbTestBtn.addEventListener("click", function () {
      dbTestBtn.disabled = true;
      dbResultEl.hidden = false;
      dbResultEl.classList.remove("ai-test-ok", "ai-test-fail");
      dbResultEl.textContent = "Đang kết nối...";

      var body = new URLSearchParams();
      body.set("db_host", dbHostInput.value.trim());
      body.set("db_port", dbPortInput.value.trim());
      body.set("db_name", dbNameInput.value.trim());
      body.set("db_username", dbUsernameInput.value.trim());
      body.set("db_password", dbPasswordInput.value);
      body.set("db_ssl_mode", dbSslModeInput ? dbSslModeInput.value : "require");
      body.set("db_connect_timeout", dbTimeoutInput ? dbTimeoutInput.value : "5");
      body.set("database_url_raw", dbUrlInput.value.trim());

      fetch("/settings/database/test", { method: "POST", credentials: "same-origin", body: body })
        .then(handleAuthRedirect)
        .then(function (response) {
          if (!response.ok) {
            return response.json().then(function (data) {
              throw new Error(data.detail || "HTTP " + response.status);
            });
          }
          return response.json();
        })
        .then(function (data) {
          showDbResult(!!data.valid, data.message || (data.valid ? "Kết nối thành công" : "Kết nối thất bại"));
        })
        .catch(function (err) {
          if (err.message === "unauthenticated") return;
          showDbResult(false, err.message);
        })
        .finally(function () {
          dbTestBtn.disabled = false;
        });

      function showDbResult(ok, msg) {
        dbResultEl.hidden = false;
        dbResultEl.classList.toggle("ai-test-ok", ok);
        dbResultEl.classList.toggle("ai-test-fail", !ok);
        dbResultEl.textContent = (ok ? "✅ " : "❌ ") + msg;
      }
    });

    // "Lưu & chuyển database" is a single blocking POST that runs all 5
    // steps documented in settings_save_database (test connection ->
    // migration -> write .env -> restart Worker/Watcher -> restart
    // Dashboard itself) before the browser gets ANY response back, so
    // there's no live per-step signal to poll from the server. This just
    // gives the operator a sense of progress for the (multi-second) wait —
    // it's a time-estimated animation, not a real backend-reported percent,
    // and deliberately caps below 100% since only the server's actual
    // response (a fresh settings.html on error, or restarting.html on
    // success) means the operation is actually done.
    var dbForm = document.getElementById("database-form");
    var dbSaveBtn = document.getElementById("db-save-btn");
    var dbProgressEl = document.getElementById("db-migrate-progress");
    var dbProgressStepEl = document.getElementById("db-migrate-step");
    var dbProgressBarFillEl = document.getElementById("db-migrate-bar-fill");
    var dbProgressBarEl = document.getElementById("db-migrate-bar");
    if (dbForm && dbSaveBtn && dbProgressEl) {
      // [percent-at-which-this-label-starts, label]
      var DB_MIGRATE_STEPS = [
        [0, "Đang kiểm tra kết nối..."],
        [15, "Đang chạy migration (tạo schema)..."],
        [40, "Đang lưu cấu hình..."],
        [55, "Đang khởi động lại Worker/Watcher..."],
        [85, "Đang khởi động lại Dashboard..."]
      ];
      var DB_MIGRATE_CAP = 97;

      dbForm.addEventListener("submit", function (event) {
        if (event.defaultPrevented) return;
        dbSaveBtn.disabled = true;
        if (dbTestBtn) dbTestBtn.disabled = true;
        dbProgressEl.hidden = false;
        // Pulses the fill continuously so parking at DB_MIGRATE_CAP for a
        // while (normal — the real work can legitimately take longer than
        // the estimate) reads as "still working", not "stuck" — see the
        // comment above DB_MIGRATE_CAP.
        dbProgressBarFillEl.classList.add("is-active");

        var percent = 0;
        var render = function () {
          var label = DB_MIGRATE_STEPS[0][1];
          for (var i = 0; i < DB_MIGRATE_STEPS.length; i++) {
            if (percent >= DB_MIGRATE_STEPS[i][0]) label = DB_MIGRATE_STEPS[i][1];
          }
          if (percent >= DB_MIGRATE_CAP) {
            label += " (vẫn đang xử lý, có thể lâu hơn dự kiến — đừng tắt/rời trang)";
          }
          dbProgressStepEl.textContent = label;
          dbProgressBarFillEl.style.width = percent + "%";
          dbProgressBarEl.setAttribute("aria-valuenow", String(Math.round(percent)));
        };
        render();
        // Native form submission navigates the page once the server responds
        // (error re-render or restarting.html) — this interval just stops
        // being relevant at that point, no need to clear it explicitly.
        setInterval(function () {
          percent = Math.min(DB_MIGRATE_CAP, percent + 1.5);
          render();
        }, 400);
      });
    }
    var dbResetForm = document.getElementById("db-reset-form");
    var dbResetBtn = document.getElementById("db-reset-btn");
    if (dbResetForm && dbResetBtn) dbResetForm.addEventListener("submit", function (event) { if (event.defaultPrevented) return; dbResetBtn.disabled = true; dbResetBtn.textContent = "Đang reset..."; });
    var dbMigrateForm = document.getElementById("db-migrate-form");
    var dbMigrateBtn = document.getElementById("db-migrate-btn");
    if (dbMigrateForm && dbMigrateBtn) dbMigrateForm.addEventListener("submit", function (event) { if (event.defaultPrevented) return; dbMigrateBtn.disabled = true; dbMigrateBtn.textContent = "Đang chạy migration..."; });
  }

  // OpenStack settings only contain node addresses; this verifies the
  // actual SSH path used later to copy Ceph config/keyring files. It uses
  // current form values and never saves or changes a remote file.
  var openstackTestBtn = document.getElementById("openstack-test-btn");
  if (openstackTestBtn) {
    var openstackForm = document.getElementById("openstack-settings-form");
    var openstackResult = document.getElementById("openstack-test-result");
    openstackTestBtn.addEventListener("click", function () {
      openstackTestBtn.disabled = true;
      openstackResult.hidden = false;
      openstackResult.className = "ai-test-result";
      openstackResult.textContent = "Đang kiểm tra kết nối...";
      fetch("/settings/openstack/test", {
        method: "POST", credentials: "same-origin", body: new FormData(openstackForm)
      })
        .then(handleAuthRedirect)
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.json();
        })
        .then(function (data) {
          openstackResult.classList.add(data.valid ? "ai-test-ok" : "ai-test-fail");
          openstackResult.textContent = (data.valid ? "✅ " : "❌ ") + data.message;
        })
        .catch(function (err) {
          if (err.message === "unauthenticated") return;
          openstackResult.classList.add("ai-test-fail");
          openstackResult.textContent = "❌ " + err.message;
        })
        .finally(function () { openstackTestBtn.disabled = false; });
    });
  }

  var openstackVmTestBtn = document.getElementById("openstack-vm-test-btn");
  if (openstackVmTestBtn) {
    var openstackVmForm = document.getElementById("openstack-settings-form");
    var openstackVmResult = document.getElementById("openstack-vm-test-result");
    openstackVmTestBtn.addEventListener("click", function () {
      openstackVmTestBtn.disabled = true;
      openstackVmResult.hidden = false;
      openstackVmResult.className = "ai-test-result";
      openstackVmResult.textContent = "Đang SSH qua Controller tới VM...";
      fetch("/settings/openstack/vm/test", {
        method: "POST", credentials: "same-origin", body: new FormData(openstackVmForm)
      })
        .then(handleAuthRedirect)
        .then(function (response) {
          if (!response.ok) throw new Error("HTTP " + response.status);
          return response.json();
        })
        .then(function (data) {
          openstackVmResult.classList.add(data.valid ? "ai-test-ok" : "ai-test-fail");
          openstackVmResult.textContent = (data.valid ? "✅ " : "❌ ") + data.message;
        })
        .catch(function (err) {
          if (err.message === "unauthenticated") return;
          openstackVmResult.classList.add("ai-test-fail");
          openstackVmResult.textContent = "❌ " + err.message;
        })
        .finally(function () { openstackVmTestBtn.disabled = false; });
    });
  }

  // Kết nối cụm Ceph: only show the fields the currently-selected "Kiểu
  // deploy" actually needs (e.g. container-name inputs are meaningless
  // under cephadm/none — see dashboard/templates/settings.html's
  // data-exec-modes attributes for which fields belong to which modes).
  var execModeSelect = document.getElementById("ceph-exec-mode-select");
  if (execModeSelect) {
    var execModeFields = Array.prototype.slice.call(document.querySelectorAll("[data-exec-modes]"));
    var applyExecModeVisibility = function () {
      var mode = execModeSelect.value;
      execModeFields.forEach(function (field) {
        var modes = field.getAttribute("data-exec-modes").split(",");
        field.hidden = modes.indexOf(mode) === -1;
      });
    };
    execModeSelect.addEventListener("change", applyExecModeVisibility);
    applyExecModeVisibility();
  }

  // Lưu trữ Backup: only show the SSH or S3 fields the currently-selected
  // "Kiểu kết nối" of THAT slot needs — same data-attribute-driven toggle
  // as the exec-mode one above, but scoped per <fieldset> since there are
  // 2 independent selects (slot A, slot B) on the same page.
  var backupTargetSlots = Array.prototype.slice.call(document.querySelectorAll(".backup-target-slot"));
  backupTargetSlots.forEach(function (slot) {
    var transportSelect = slot.querySelector(".backup-transport-select");
    if (!transportSelect) return;
    var transportFields = Array.prototype.slice.call(slot.querySelectorAll("[data-backup-transport]"));
    var applyTransportVisibility = function () {
      var transport = transportSelect.value;
      transportFields.forEach(function (field) {
        field.hidden = field.getAttribute("data-backup-transport") !== transport;
      });
    };
    transportSelect.addEventListener("change", applyTransportVisibility);
    applyTransportVisibility();
  });

  // Settings sidebar (2026-07-24) — one section-panel visible at a time.
  // The server already picks which panel starts visible (settings.py's
  // _compute_active_section, so a form's error/success message after a
  // POST always lands on the right panel, not hidden behind whichever one
  // happened to be first) — this just handles CLICKING a different item
  // without a page reload.
  var settingsNavItems = Array.prototype.slice.call(document.querySelectorAll(".settings-nav-item"));
  var settingsPanels = Array.prototype.slice.call(document.querySelectorAll(".settings-panel"));
  var settingsGroups = Array.prototype.slice.call(document.querySelectorAll(".settings-nav-group"));
  settingsGroups.forEach(function (group) {
    var toggle = group.querySelector(".settings-nav-group-toggle");
    var items = group.querySelector(".settings-nav-group-items");
    if (!toggle || !items) return;
    var storageKey = "settingsGroup:" + group.getAttribute("data-settings-group");
    var hasActiveItem = !!items.querySelector(".settings-nav-item.active");
    var saved = null;
    try { saved = localStorage.getItem(storageKey); } catch (e) { /* ignore */ }
    var expanded = hasActiveItem || saved === "1";
    if (!hasActiveItem && saved === "0") expanded = false;
    items.hidden = !expanded;
    toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
    toggle.addEventListener("click", function () {
      expanded = items.hidden;
      items.hidden = !expanded;
      toggle.setAttribute("aria-expanded", expanded ? "true" : "false");
      try { localStorage.setItem(storageKey, expanded ? "1" : "0"); } catch (e) { /* ignore */ }
    });
  });
  if (settingsNavItems.length && settingsPanels.length) {
    window.__settingsNavigationReady = true;
    settingsNavItems.forEach(function (item) {
      item.addEventListener("click", function () {
        var section = item.getAttribute("data-section");
        settingsNavItems.forEach(function (other) {
          other.classList.toggle("active", other === item);
        });
        settingsPanels.forEach(function (panel) {
          panel.hidden = panel.getAttribute("data-panel") !== section;
        });
        var parentItems = item.closest(".settings-nav-group-items");
        if (parentItems) {
          parentItems.hidden = false;
          var parentToggle = parentItems.parentElement.querySelector(".settings-nav-group-toggle");
          if (parentToggle) parentToggle.setAttribute("aria-expanded", "true");
        }
      });
    });

    // 2026-08-04: lets another page (e.g. telegram_help.html's "Quay lại
    // Settings" link) deep-link straight to one section via
    // "/settings#<data-section>" — the server-picked panel above still
    // wins on a plain "/settings" load (no hash), this only overrides it
    // when a hash naming a REAL section is actually present, so a stray
    // "#" (or one from an unrelated future feature) never blanks every
    // panel by matching nothing.
    var initialSection = window.location.hash.replace(/^#/, "");
    var initialItem = settingsNavItems.filter(function (item) {
      return item.getAttribute("data-section") === initialSection;
    })[0];
    if (initialItem) {
      initialItem.click();
    }
    window.addEventListener("hashchange", function () {
      var section = window.location.hash.replace(/^#/, "");
      var item = settingsNavItems.filter(function (candidate) {
        return candidate.getAttribute("data-section") === section;
      })[0];
      if (item) item.click();
    });
  }

  // Settings control-plane navigation on mobile.
  var controlMenuToggle = document.querySelector(".control-menu-toggle");
  if (controlMenuToggle) {
    controlMenuToggle.addEventListener("click", function () {
      var open = document.body.classList.toggle("control-nav-open");
      controlMenuToggle.setAttribute("aria-expanded", open ? "true" : "false");
    });
    document.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        document.body.classList.remove("control-nav-open");
        controlMenuToggle.setAttribute("aria-expanded", "false");
      }
    });
  }

  // Service restart confirmation uses the existing POST forms/endpoints.
  // Only the presentation is enhanced: confirmation is contextual and the
  // initiating button exposes an in-progress state before navigation.
  var restartDialog = document.getElementById("restart-service-dialog");
  var pendingRestartButton = null;
  if (restartDialog && typeof restartDialog.showModal === "function") {
    var dialogTitle = document.getElementById("restart-dialog-title");
    var dialogDescription = document.getElementById("restart-dialog-description");
    var dialogConfirm = restartDialog.querySelector("[data-dialog-confirm]");
    var dialogCancel = restartDialog.querySelector("[data-dialog-cancel]");
    document.querySelectorAll("[data-restart-service]").forEach(function (button) {
      button.addEventListener("click", function () {
        pendingRestartButton = button;
        var service = button.getAttribute("data-restart-service");
        dialogTitle.textContent = "Khởi động lại " + service + "?";
        dialogDescription.textContent = button.getAttribute("data-restart-warning") || "This service will be briefly unavailable.";
        dialogConfirm.textContent = "Xác nhận";
        restartDialog.showModal();
      });
    });
    dialogCancel.addEventListener("click", function () { restartDialog.close(); });
    restartDialog.addEventListener("click", function (event) {
      if (event.target === restartDialog) restartDialog.close();
    });
    dialogConfirm.addEventListener("click", function () {
      if (!pendingRestartButton) return;
      var service = pendingRestartButton.getAttribute("data-restart-service");
      var form = document.getElementById("restart-form-" + service.toLowerCase());
      if (!form) return;
      dialogConfirm.disabled = true;
      dialogConfirm.textContent = "Đang xử lý...";
      pendingRestartButton.classList.add("is-loading");
      pendingRestartButton.innerHTML = "<span>↻</span>Đang xử lý...";
      form.submit();
    });
  }
})();

// --- Patch pipeline: compact command input, validation and explicit restart flow ---
(function () {
  var form = document.getElementById("patch-pipeline-form");
  if (!form) return;

  var commandInput = document.getElementById("pipeline-build-command");
  var actionInput = document.getElementById("pipeline-save-action");
  var statusChip = document.querySelector(".pipeline-status-chip");
  var fields = [
    { id: "pipeline-build-node", message: "Nhập IP hoặc hostname của build server." },
    { id: "pipeline-source-dir", message: "Đường dẫn phải bắt đầu bằng '/'." },
    { id: "pipeline-build-command", message: "Nhập lệnh build." },
    { id: "pipeline-output-dir", message: "Đường dẫn phải bắt đầu bằng '/'." },
    { id: "pipeline-staging-dir", message: "Đường dẫn phải bắt đầu bằng '/'." },
  ];

  function errorNode(id) {
    return form.querySelector('[data-error-for="' + id + '"]');
  }

  function setError(id, message) {
    var node = errorNode(id);
    if (node) node.textContent = message || "";
    var input = document.getElementById(id);
    if (input) input.setAttribute("aria-invalid", message ? "true" : "false");
  }

  function validHost(value) {
    var hostname = /^(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$/;
    var parts = value.split(".");
    var dottedQuad = /^\d+(?:\.\d+){3}$/.test(value);
    if (dottedQuad) return parts.every(function (part) {
      return /^\d{1,3}$/.test(part) && Number(part) >= 0 && Number(part) <= 255;
    });
    return hostname.test(value) || value.indexOf(":") !== -1;
  }

  function validate() {
    var valid = true;
    fields.forEach(function (field) {
      var input = document.getElementById(field.id);
      var value = input ? input.value.trim() : "";
      var message = "";
      if (!value) message = field.message;
      else if (field.id === "pipeline-build-node" && !validHost(value)) {
        message = "IP/hostname không hợp lệ. Ví dụ: 10.0.0.20 hoặc build.ceph.local.";
      } else if (field.id !== "pipeline-build-node" && field.id !== "pipeline-build-command" && value.charAt(0) !== "/") {
        message = field.message;
      }
      setError(field.id, message);
      if (message) valid = false;
    });
    return valid;
  }

  function resizeCommand() {
    if (!commandInput) return;
    commandInput.style.height = "auto";
    var maxHeight = 220;
    commandInput.style.height = Math.min(commandInput.scrollHeight, maxHeight) + "px";
    commandInput.style.overflowY = commandInput.scrollHeight > maxHeight ? "auto" : "hidden";
  }

  if (commandInput) {
    commandInput.addEventListener("input", resizeCommand);
    resizeCommand();
  }

  form.addEventListener("submit", function (event) {
    if (!validate()) {
      event.preventDefault();
      return;
    }
    var submitter = event.submitter;
    var action = submitter && submitter.getAttribute("data-save-action") === "save" ? "save" : "save-restart";
    if (actionInput) actionInput.value = action;
    if (action === "save-restart" && !window.confirm("Lưu cấu hình pipeline và khởi động lại Worker? Service sẽ tạm gián đoạn vài giây.")) {
      event.preventDefault();
      return;
    }
    if (submitter) {
      submitter.disabled = true;
      submitter.classList.add("is-loading");
      submitter.textContent = action === "save-restart" ? "Đang lưu & restart…" : "Đang lưu…";
    }
  });

  function updateStatus(data) {
    if (!statusChip) return;
    var label = statusChip.querySelector("span");
    var detail = statusChip.querySelector("small");
    ["is-ready", "is-missing", "is-running", "is-error", "is-waiting"].forEach(function (name) {
      statusChip.classList.remove(name);
    });
    var visualState = data.state === "not_configured" ? "missing" : (data.state || (data.configured ? "ready" : "missing"));
    statusChip.classList.add("is-" + visualState);
    if (label) label.textContent = data.state_label || (data.configured ? "Sẵn sàng chạy pipeline" : "Chưa cấu hình đầy đủ");
    if (detail) detail.textContent = data.last_build ? "Build gần nhất: " + data.last_build : "Build gần nhất: chưa ghi nhận";
  }

  function refreshStatus() {
    fetch("/api/settings/patch-pipeline/status", { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("status unavailable");
        return response.json();
      })
      .then(updateStatus)
      .catch(function () { /* Keep the server-rendered fallback status. */ });
  }

  refreshStatus();
  window.setInterval(refreshStatus, 30000);
})();

(function () {
  var groupLink = document.getElementById("settings-breadcrumb-group");
  var currentLabel = document.getElementById("settings-breadcrumb-current");
  var items = Array.prototype.slice.call(document.querySelectorAll(".settings-nav-item[data-section]"));
  if (!groupLink || !currentLabel || !items.length) return;
  var labels = {
    "restart-controls": ["Hệ thống", "Tiến trình hệ thống"], "action-policy": ["Hệ thống", "Chính sách hành động AI"],
    database: ["Hệ thống", "Kết nối cơ sở dữ liệu"], "server-log": ["Hệ thống", "Nhật ký máy chủ"],
    "patch-pipeline": ["Pipeline & lưu trữ", "Pipeline"], "log-intel": ["Pipeline & lưu trữ", "Phân tích nhật ký"],
    "dual-ai": ["Pipeline & lưu trữ", "Hai AI trao đổi"], "code-repair": ["Pipeline & lưu trữ", "Sửa mã bằng AI"],
    "backup-targets": ["Pipeline & lưu trữ", "Cấu hình lưu trữ"], router: ["Kết nối", "API AI"],
    cost: ["Kết nối", "Chi phí"], cluster: ["Kết nối", "Cụm Ceph"], "ceph-host-keys": ["Kết nối", "Khóa SSH node Ceph"],
    openstack: ["Kết nối", "OpenStack"], cleanup: ["Bảo trì", "Bảo trì hệ thống"]
  };
  function update(item) {
    var data = labels[item.getAttribute("data-section")] || ["Cài đặt", item.textContent.trim()];
    groupLink.textContent = data[0]; groupLink.href = "#" + item.getAttribute("data-section"); currentLabel.textContent = data[1];
  }
  items.forEach(function (item) { item.addEventListener("click", function () { update(item); }); });
  update(items.filter(function (item) { return item.classList.contains("active"); })[0] || items[0]);
})();

// --- AI Code Repair role model catalog ------------------------------------
// Planner/Reviewer and Implementer have independent provider/model fields.
// Reuse the authenticated account status endpoints so the list reflects the
// actual Codex account (and the server's Claude catalog), not a stale hardcode.
(function () {
  var plannerProvider = document.getElementById("code-repair-planner-provider");
  var implementerProvider = document.getElementById("code-repair-implementer-provider");
  if (!plannerProvider || !implementerProvider) return;

  var catalogs = {};

  function requestCatalog(provider) {
    if (catalogs[provider]) return catalogs[provider];
    var url = provider === "codex" ? "/settings/codex/status" : "/settings/claude/status";
    catalogs[provider] = fetch(url, { credentials: "same-origin" }).then(function (response) {
      if (!response.ok) {
        return response.json().then(function (data) {
          throw new Error(data.detail || "HTTP " + response.status);
        });
      }
      return response.json();
    });
    return catalogs[provider];
  }

  function addOption(select, value, label, selected) {
    var option = document.createElement("option");
    option.value = value;
    option.textContent = label;
    option.selected = selected;
    select.appendChild(option);
  }

  function renderModelSelect(select, status, data, preserveCurrent) {
    var current = preserveCurrent ? (select.dataset.currentModel || "") : "";
    var models = data && Array.isArray(data.models) ? data.models : [];
    var seen = {};
    select.innerHTML = "";
    addOption(select, "", "Tự động (model mặc định)", !current);
    models.forEach(function (item) {
      var id = typeof item === "string" ? item : item.id;
      if (!id || seen[id]) return;
      seen[id] = true;
      var label = typeof item === "string" ? item : (item.label || id);
      if (item && item.version) label += " " + item.version;
      if (item && item.is_default) label += " · mặc định";
      addOption(select, id, label, id === current);
    });
    if (current && !seen[current]) {
      addOption(select, current, current + " · đã lưu", true);
    }
    select.value = current;
    if (status) {
      if (models.length) {
        status.textContent = "Đã tải " + models.length + " model từ " + select.dataset.providerName + ".";
        status.className = "hint success";
      } else if (data && data.authenticated === false) {
        status.textContent = "Chưa đăng nhập " + select.dataset.providerName + "; có thể dùng model mặc định hoặc nhập cấu hình sau.";
        status.className = "hint";
      } else {
        status.textContent = "Provider chưa trả danh sách model; đang dùng model mặc định.";
        status.className = "hint";
      }
    }
  }

  function loadRole(providerSelect, modelSelect, status, preserveCurrent) {
    var provider = providerSelect.value;
    modelSelect.dataset.providerName = provider === "codex" ? "Codex" : "Claude";
    if (provider === "auto") {
      renderModelSelect(modelSelect, status, { models: [] }, preserveCurrent);
      status.textContent = "auto sẽ chọn provider khả dụng; chọn Codex hoặc Claude để xem catalog model.";
      status.className = "hint";
      return;
    }
    status.textContent = "Đang tải danh sách model " + modelSelect.dataset.providerName + "…";
    status.className = "hint";
    requestCatalog(provider).then(function (data) {
      renderModelSelect(modelSelect, status, data, preserveCurrent);
    }).catch(function (error) {
      renderModelSelect(modelSelect, status, { models: [] }, preserveCurrent);
      status.textContent = "Không tải được catalog " + modelSelect.dataset.providerName + ": " + error.message;
      status.className = "hint error";
    });
  }

  function bindRole(providerSelect, modelId, statusId) {
    var modelSelect = document.getElementById(modelId);
    var status = document.getElementById(statusId);
    if (!modelSelect || !status) return;
    providerSelect.addEventListener("change", function () {
      // A model selected for one provider must not silently be submitted for
      // another provider. The operator can then choose from the new catalog.
      modelSelect.dataset.currentModel = "";
      loadRole(providerSelect, modelSelect, status, false);
    });
    loadRole(providerSelect, modelSelect, status, true);
  }

  bindRole(plannerProvider, "code-repair-planner-model", "code-repair-planner-model-status");
  bindRole(implementerProvider, "code-repair-implementer-model", "code-repair-implementer-model-status");

})();

// --- AI Code Repair account source flow -----------------------------------
// The operator chooses the account source first. Configured roles reuse the
// AI API account; separate roles get their own Codex/Claude credential home.
(function () {
  function bindAccount(role) {
    var source = document.getElementById("code-repair-" + role + "-account-source");
    var configured = document.getElementById("code-repair-" + role + "-configured");
    var separate = document.getElementById("code-repair-" + role + "-separate");
    var provider = document.getElementById("code-repair-" + role + "-separate-provider");
    var model = document.getElementById("code-repair-" + role + "-separate-model");
    var profile = document.getElementById("code-repair-" + role + "-account-profile");
    var status = document.getElementById("code-repair-" + role + "-separate-status");
    var login = document.getElementById("code-repair-" + role + "-separate-login");
    var logout = document.getElementById("code-repair-" + role + "-separate-logout");
    var flow = document.getElementById("code-repair-" + role + "-separate-flow");
    var link = document.getElementById("code-repair-" + role + "-separate-link");
    var code = document.getElementById("code-repair-" + role + "-separate-code");
    var codeWrap = document.getElementById("code-repair-" + role + "-separate-code-wrap");
    var authWrap = document.getElementById("code-repair-" + role + "-separate-auth-wrap");
    var authCode = document.getElementById("code-repair-" + role + "-separate-auth-code");
    var complete = document.getElementById("code-repair-" + role + "-separate-complete");
    var pollTimer = null;
    if (!source || !configured || !separate) return;

    function request(url, options) {
      return fetch(url, Object.assign({ credentials: "same-origin" }, options || {})).then(function (response) {
        if (!response.ok) return response.json().then(function (data) { throw new Error(data.detail || "HTTP " + response.status); });
        return response.json();
      });
    }
    function setStatus(text, className) {
      if (status) { status.textContent = text; status.className = "hint" + (className ? " " + className : ""); }
    }
    function profileReady() { return provider && profile && provider.value && profile.value.trim(); }
    function refresh() {
      if (!profileReady()) { setStatus("Nhập profile để kiểm tra tài khoản."); if (logout) logout.hidden = true; return; }
      setStatus("Đang kiểm tra tài khoản riêng...");
      request("/settings/code-repair/account/status?provider=" + encodeURIComponent(provider.value) + "&profile=" + encodeURIComponent(profile.value.trim()))
        .then(function (data) {
          if (data.authenticated) {
            setStatus("✅ Đã đăng nhập " + (data.email || "tài khoản " + provider.value), "success");
            if (login) login.hidden = true;
            if (logout) logout.hidden = false;
          } else {
            setStatus(data.installed === false ? "⚠️ Chưa cài " + provider.value + " CLI trên server." : "Chưa đăng nhập tài khoản riêng.");
            if (login) login.hidden = false;
            if (logout) logout.hidden = true;
          }
        }).catch(function (error) { setStatus("❌ " + error.message, "error"); });
    }
    function toggle() {
      var isSeparate = source.value === "separate";
      configured.hidden = isSeparate;
      separate.hidden = !isSeparate;
      configured.querySelectorAll("input, select").forEach(function (element) { element.disabled = isSeparate; });
      separate.querySelectorAll("input, select").forEach(function (element) { element.disabled = !isSeparate; });
      if (isSeparate) refresh();
    }
    function startLogin() {
      if (!profileReady()) { setStatus("Cần nhập profile trước khi đăng nhập.", "error"); if (profile) profile.focus(); return; }
      login.disabled = true;
      setStatus("Đang tạo phiên đăng nhập...");
      var body = new URLSearchParams(); body.set("provider", provider.value); body.set("profile", profile.value.trim());
      request("/settings/code-repair/account/login/start", { method: "POST", body: body }).then(function (data) {
        if (link) { link.href = data.verification_url; link.textContent = data.provider === "codex" ? "trang xác thực Codex" : "trang xác thực Claude"; }
        if (code) code.textContent = data.user_code || "";
        if (codeWrap) codeWrap.hidden = data.provider !== "codex";
        if (authWrap) authWrap.hidden = data.provider !== "claude";
        if (complete) complete.hidden = data.provider !== "claude";
        if (flow) flow.hidden = false;
        if (data.verification_url) window.open(data.verification_url, "_blank", "noopener");
        setStatus("Đang chờ hoàn tất đăng nhập...");
        if (data.provider === "codex") {
          clearInterval(pollTimer);
          pollTimer = setInterval(function () { refresh(); }, 2500);
        }
      }).catch(function (error) { setStatus("❌ " + error.message, "error"); login.disabled = false; });
    }
    function completeLogin() {
      complete.disabled = true;
      var body = new URLSearchParams(); body.set("provider", provider.value); body.set("profile", profile.value.trim()); body.set("authentication_code", (authCode.value || "").trim());
      request("/settings/code-repair/account/login/complete", { method: "POST", body: body }).then(function () {
        if (flow) flow.hidden = true; refresh();
      }).catch(function (error) { setStatus("❌ " + error.message, "error"); }).then(function () { complete.disabled = false; });
    }
    function logoutAccount() {
      var body = new URLSearchParams(); body.set("provider", provider.value); body.set("profile", profile.value.trim());
      logout.disabled = true;
      request("/settings/code-repair/account/logout", { method: "POST", body: body }).then(function () { if (flow) flow.hidden = true; refresh(); })
        .catch(function (error) { setStatus("❌ " + error.message, "error"); }).then(function () { logout.disabled = false; });
    }
    source.addEventListener("change", toggle);
    if (provider) provider.addEventListener("change", refresh);
    if (profile) profile.addEventListener("blur", refresh);
    if (login) login.addEventListener("click", startLogin);
    if (complete) complete.addEventListener("click", completeLogin);
    if (logout) logout.addEventListener("click", logoutAccount);
    toggle();
  }
  bindAccount("planner");
  bindAccount("implementer");
})();

// AI Action Policy: compact local filtering with custom dark dropdowns.
(function () {
  var table = document.getElementById("action-policy-table");
  var tableWrap = document.getElementById("action-policy-table-wrap");
  var search = document.getElementById("action-policy-search");
  var classification = document.getElementById("action-policy-classification-filter");
  var source = document.getElementById("action-policy-source-filter");
  var reset = document.getElementById("action-policy-filter-reset");
  var result = document.getElementById("action-policy-filter-result");
  var empty = document.getElementById("action-policy-filter-empty");
  var pagination = document.getElementById("action-policy-pagination");
  var previous = document.getElementById("action-policy-page-previous");
  var next = document.getElementById("action-policy-page-next");
  var pageStatus = document.getElementById("action-policy-page-status");
  var pageButtons = document.getElementById("action-policy-page-buttons");
  var pageSummary = document.getElementById("action-policy-page-summary");
  if (!table || !search || !classification || !source || !reset || !result || !empty ||
      !pagination || !previous || !next || !pageStatus || !pageButtons || !pageSummary) return;

  var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr"));
  var pageSize = 10;
  var currentPage = 1;
  rows.sort(function (left, right) {
    return String(left.getAttribute("data-action-id") || "").localeCompare(
      String(right.getAttribute("data-action-id") || ""), undefined, { sensitivity: "base" }
    );
  });
  rows.forEach(function (row) { table.tBodies[0].appendChild(row); });
  function normalize(value) { return String(value || "").trim().toLowerCase(); }
  function filterValue(control) { return control.getAttribute("data-filter-value") || ""; }
  function closeMenus(except) { document.querySelectorAll("[data-filter-menu], [data-policy-menu], [data-bulk-menu]").forEach(function (menu) { if (menu !== except) menu.hidden = true; }); }
  function bindFilter(control) {
    var trigger = control.querySelector("[data-filter-trigger]");
    var menu = control.querySelector("[data-filter-menu]");
    if (!trigger || !menu) return;
    trigger.addEventListener("click", function (event) { event.stopPropagation(); var open = menu.hidden; closeMenus(menu); menu.hidden = !open; });
    menu.querySelectorAll("[data-filter-option]").forEach(function (option) { option.addEventListener("click", function () { control.setAttribute("data-filter-value", option.getAttribute("data-filter-option") || ""); control.querySelector("[data-filter-label]").textContent = option.getAttribute("data-label") || option.textContent.trim(); menu.hidden = true; resetPageAndRender(); }); });
  }
  bindFilter(classification); bindFilter(source);
  document.addEventListener("click", function () { closeMenus(null); });
  function render() {
    var query = normalize(search.value);
    var wantedClassification = filterValue(classification);
    var wantedSource = filterValue(source);
    var filtered = rows.filter(function (row) {
      return (!query || normalize(row.getAttribute("data-action-id")).indexOf(query) !== -1) &&
        (!wantedClassification || row.getAttribute("data-classification") === wantedClassification) &&
        (!wantedSource || row.getAttribute("data-policy-source") === wantedSource);
    });
    var pageCount = Math.max(1, Math.ceil(filtered.length / pageSize));
    currentPage = Math.min(currentPage, pageCount);
    var first = (currentPage - 1) * pageSize;
    var pageRows = filtered.slice(first, first + pageSize);
    rows.forEach(function (row) { row.hidden = pageRows.indexOf(row) === -1; });
    result.textContent = filtered.length + " / " + rows.length + " hành động";
    var summaryText = "Hiển thị " + (filtered.length ? first + 1 : 0) + "–" + Math.min(first + pageSize, filtered.length) + " / " + filtered.length + " mục";
    pageSummary.textContent = summaryText;
    pageSummary.dataset.mobileSummary = (filtered.length ? first + 1 : 0) + "–" + Math.min(first + pageSize, filtered.length) + " / " + filtered.length;
    empty.hidden = filtered.length !== 0;
    if (tableWrap) tableWrap.hidden = filtered.length === 0;
    pagination.hidden = filtered.length === 0;
    pageStatus.textContent = "Trang " + currentPage + "/" + pageCount;
    previous.disabled = currentPage <= 1;
    next.disabled = currentPage >= pageCount;
    if (window.DashboardPagination) window.DashboardPagination.renderPages(pageButtons, currentPage, pageCount, function (target) { currentPage = target; render(); });
    reset.disabled = !query && !wantedClassification && !wantedSource;
  }
  function resetPageAndRender() { currentPage = 1; render(); }
  search.addEventListener("input", resetPageAndRender);
  classification.addEventListener("change", resetPageAndRender);
  source.addEventListener("change", resetPageAndRender);
  previous.addEventListener("click", function () {
    if (currentPage > 1) { currentPage -= 1; render(); }
  });
  next.addEventListener("click", function () {
    currentPage += 1;
    render();
  });
  reset.addEventListener("click", function () {
    search.value = "";
    classification.setAttribute("data-filter-value", "");
    source.setAttribute("data-filter-value", "");
    classification.querySelector("[data-filter-label]").textContent = "Tất cả";
    source.querySelector("[data-filter-label]").textContent = "Tất cả";
    resetPageAndRender();
    search.focus();
  });
  var selectAll = document.getElementById("action-policy-select-all");
  var rowChecks = Array.prototype.slice.call(document.querySelectorAll("[data-action-select]"));
  var bulkToolbar = document.getElementById("action-policy-bulk-toolbar");
  var selectedCount = document.getElementById("action-policy-selected-count");
  var bulkApply = document.getElementById("action-policy-bulk-apply");
  var bulkValue = document.getElementById("action-policy-bulk-value");
  function selectedRows() { return rowChecks.filter(function (check) { return check.checked; }); }
  function bulkPolicy() { return bulkValue ? (bulkValue.getAttribute("data-policy-value") || "") : ""; }
  function updateBulk() { var count = selectedRows().length; if (bulkToolbar) bulkToolbar.hidden = count === 0; if (selectedCount) selectedCount.textContent = "Đã chọn " + count + " action"; if (bulkApply) bulkApply.disabled = count === 0 || !bulkPolicy(); if (selectAll) selectAll.checked = count > 0 && count === rowChecks.length; }
  rowChecks.forEach(function (check) { check.addEventListener("change", updateBulk); });
  if (selectAll) selectAll.addEventListener("change", function () { rowChecks.forEach(function (check) { check.checked = selectAll.checked; }); updateBulk(); });
  if (bulkValue) {
    var bulkTrigger = bulkValue.querySelector("[data-bulk-trigger]");
    var bulkMenu = bulkValue.querySelector("[data-bulk-menu]");
    if (bulkTrigger && bulkMenu) {
      bulkTrigger.addEventListener("click", function (event) { event.stopPropagation(); bulkMenu.hidden = !bulkMenu.hidden; });
      bulkMenu.querySelectorAll("[data-bulk-option]").forEach(function (option) { option.addEventListener("click", function () { bulkValue.setAttribute("data-policy-value", option.getAttribute("data-bulk-option")); bulkValue.querySelector("[data-bulk-label]").textContent = "Đổi tất cả thành " + option.getAttribute("data-bulk-option"); bulkMenu.hidden = true; updateBulk(); }); });
    }
  }
  if (bulkApply) bulkApply.addEventListener("click", function () {
    var selected = selectedRows(); var policy = bulkPolicy(); if (!selected.length || !policy) return;
    if (!window.confirm("Đổi policy của " + selected.length + " action thành " + policy + "?")) return;
    bulkApply.disabled = true; bulkApply.textContent = "Đang áp dụng...";
    fetch("/settings/autopilot/action-policy/bulk", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action_ids: selected.map(function (check) { return check.closest("tr").getAttribute("data-action-id"); }), classification: policy, confirmation: "OK" }) })
      .then(function (response) { if (!response.ok) return response.json().then(function (data) { throw new Error(data.detail || "HTTP " + response.status); }); return response.json(); })
      .then(function () { window.location.reload(); }).catch(function (error) { window.alert("Không thể áp dụng policy: " + error.message); bulkApply.disabled = false; bulkApply.textContent = "Áp dụng"; });
  });
  render();
})();

// Policy changes use a clear confirmation naming both the action and target.
(function () {
  var forms = document.querySelectorAll(".action-policy-change-form");
  Array.prototype.forEach.call(forms, function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var actionName = form.getAttribute("data-action-name") || "action";
      var value = form.querySelector("[data-policy-value]");
      var classification = value ? value.value : "";
      if (!window.confirm("Đổi policy của " + actionName + " thành " + classification + "?")) return;
      var field = form.querySelector('input[name="confirmation"]');
      if (!field) return;
      field.value = "OK";
      form.submit();
    });
    var trigger = form.querySelector("[data-policy-trigger]");
    var menu = form.querySelector("[data-policy-menu]");
    var value = form.querySelector("[data-policy-value]");
    var label = form.querySelector("[data-policy-label]");
    if (trigger && menu && value && label) {
      trigger.addEventListener("click", function (event) { event.stopPropagation(); document.querySelectorAll("[data-policy-menu]").forEach(function (item) { if (item !== menu) item.hidden = true; }); menu.hidden = !menu.hidden; });
      menu.querySelectorAll("[data-policy-option]").forEach(function (option) { option.addEventListener("click", function () { value.value = option.getAttribute("data-policy-option"); label.textContent = value.value; label.className = "policy-" + value.value.toLowerCase(); form.closest("tr").classList.add("is-policy-changed"); menu.hidden = true; }); });
    }
  });
})();

(function () {
  var forms = document.querySelectorAll(".promotion-approval-form");
  Array.prototype.forEach.call(forms, function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var confirmation = window.prompt("Nhập OK để duyệt playbook lên L3");
      if (confirmation !== "OK") return;
      form.querySelector('input[name="confirmation"]').value = confirmation;
      form.submit();
    });
  });
})();

// --- Log Intelligence: kiểm tra kết nối Loki trước khi lưu ----------------
// Cùng khuôn "test kết nối" mà form Database/OpenStack đã dùng: gọi thẳng
// endpoint test, không lưu gì.
(function () {
  var testBtn = document.getElementById("log-intel-test-loki");
  if (!testBtn) return;
  var urlInput = document.getElementById("log-intel-loki-url");
  var tenantInput = document.getElementById("log-intel-loki-tenant");
  var result = document.getElementById("log-intel-test-loki-result");

  // Bản sao cục bộ: `handleAuthRedirect` gốc nằm trong IIFE phía trên và
  // không với tới được từ đây. Tham chiếu xuyên scope làm chuỗi promise ném
  // ReferenceError ngay lúc dựng — trước cả khi .catch()/.finally() kịp gắn
  // vào — nên nút kẹt disabled và chữ "Đang kiểm tra…" đứng mãi.
  function handleAuthRedirect(response) {
    if (response.redirected && response.url.indexOf("/login") !== -1) {
      window.location.reload();
      throw new Error("unauthenticated");
    }
    return response;
  }

  function show(ok, msg) {
    result.textContent = (ok ? "✅ " : "❌ ") + msg;
    result.className = ok ? "success" : "error";
  }

  testBtn.addEventListener("click", function () {
    if (!urlInput.value.trim()) {
      show(false, "Chưa điền Loki URL.");
      return;
    }
    testBtn.disabled = true;
    result.textContent = "Đang kiểm tra…";
    result.className = "muted";

    var body = new URLSearchParams();
    body.set("log_intel_loki_url", urlInput.value.trim());
    body.set("log_intel_loki_tenant", tenantInput ? tenantInput.value.trim() : "");

    fetch("/settings/log-intel/test-loki", {
      method: "POST",
      credentials: "same-origin",
      body: body,
    })
      .then(handleAuthRedirect)
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (data) {
            throw new Error(data.detail || "HTTP " + response.status);
          });
        }
        return response.json();
      })
      .then(function (data) {
        show(!!data.ok, data.message || (data.ok ? "Kết nối thành công" : "Kết nối thất bại"));
      })
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
        show(false, err.message);
      })
      .finally(function () {
        testBtn.disabled = false;
      });
  });
})();

// AI API only: replace the model selectors inside the router panel with dark-theme controls.
(function () {
  var routerPanel = document.querySelector('[data-panel="router"]');
  if (!routerPanel) return;
  var ids = ["codex-model-select", "claude-model-select", "claude-effort-select", "router-model-select"];

  function closeAll(except) {
    routerPanel.querySelectorAll(".settings-custom-select-menu").forEach(function (menu) {
      if (menu !== except) menu.hidden = true;
    });
    routerPanel.querySelectorAll(".settings-custom-select-trigger").forEach(function (trigger) {
      if (!except || trigger.nextElementSibling !== except) trigger.setAttribute("aria-expanded", "false");
    });
  }

  function sync(select, trigger, menu) {
    var current = select.options[select.selectedIndex];
    trigger.textContent = current ? current.textContent : "Chọn...";
    menu.innerHTML = "";
    Array.prototype.forEach.call(select.options, function (option) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "settings-custom-select-option";
      item.textContent = option.textContent;
      item.setAttribute("role", "option");
      item.setAttribute("aria-selected", option.selected ? "true" : "false");
      item.addEventListener("click", function () {
        select.value = option.value;
        select.dispatchEvent(new Event("change", { bubbles: true }));
        sync(select, trigger, menu);
        menu.hidden = true;
        trigger.setAttribute("aria-expanded", "false");
        trigger.focus();
      });
      menu.appendChild(item);
    });
  }

  function init(select) {
    if (!select || select.closest('[data-panel="router"]') !== routerPanel || select.dataset.customSelectReady) return;
    select.dataset.customSelectReady = "true";
    var wrapper = document.createElement("span");
    wrapper.className = "settings-custom-select";
    var trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "settings-custom-select-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    var menu = document.createElement("span");
    menu.className = "settings-custom-select-menu";
    menu.setAttribute("role", "listbox");
    menu.hidden = true;
    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(select);
    wrapper.appendChild(trigger);
    wrapper.appendChild(menu);
    select.classList.add("settings-native-select");
    trigger.addEventListener("click", function () {
      var opening = menu.hidden;
      closeAll(menu);
      menu.hidden = !opening;
      trigger.setAttribute("aria-expanded", opening ? "true" : "false");
    });
    select.addEventListener("change", function () { sync(select, trigger, menu); });
    new MutationObserver(function () { sync(select, trigger, menu); }).observe(select, { childList: true });
    sync(select, trigger, menu);
  }

  ids.forEach(function (id) { init(document.getElementById(id)); });
  routerPanel.addEventListener("click", function (event) {
    if (!event.target.closest(".settings-custom-select")) closeAll(null);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeAll(null);
  });
})();

// AI API only: switch service content without reloading the Settings page.
(function () {
  var panel = document.querySelector('[data-panel="router"]');
  if (!panel) return;
  var tabs = Array.prototype.slice.call(panel.querySelectorAll("[data-ai-tab]"));
  var tabPanels = Array.prototype.slice.call(panel.querySelectorAll("[data-ai-tab-panel]"));
  var sharedConfig = panel.querySelector("#router-shared-config");
  var warning = panel.querySelector("#ai-service-tab-warning");
  var baseUrl = panel.querySelector("#router-base-url-input");
  var apiKey = panel.querySelector("#router-api-key-input");
  var step2Provider = panel.querySelector("#router-step2-provider");
  var step2Form = panel.querySelector("#router-step2-form");
  var configState = {};
  var dirty = {};
  var suppressDirty = false;
  var routerConfiguredProvider = panel.getAttribute("data-router-configured-provider") || "";
  function accountWasConfigured(provider) {
    try { return window.localStorage.getItem("ceph-ai-ai-account-configured-" + provider) === "true"; }
    catch (error) { return false; }
  }
  function rememberAccountConfigured(provider) {
    try { window.localStorage.setItem("ceph-ai-ai-account-configured-" + provider, "true"); }
    catch (error) { /* localStorage can be unavailable in private browsing */ }
  }
  var accountState = {
    openai: {
      enabled: panel.getAttribute("data-codex-enabled") === "true",
      configured: panel.getAttribute("data-codex-enabled") === "true" || accountWasConfigured("openai")
    },
    anthropic: {
      enabled: panel.getAttribute("data-claude-enabled") === "true",
      configured: panel.getAttribute("data-claude-enabled") === "true" || accountWasConfigured("anthropic")
    }
  };
  var active = (tabs.filter(function (tab) { return tab.classList.contains("is-active"); })[0] || tabs[0]);
  var activeProvider = active ? active.getAttribute("data-ai-tab") : "9router";

  function findRadio(provider) {
    return panel.querySelector('input[name="router_provider"][value="' + provider + '"]');
  }

  function remember(provider) {
    if (!baseUrl || !apiKey) return;
    configState[provider] = { baseUrl: baseUrl.value, apiKey: apiKey.value };
  }

  function setProvider(provider) {
    var radio = findRadio(provider);
    if (!radio) return;
    suppressDirty = true;
    radio.checked = true;
    radio.dispatchEvent(new Event("change", { bubbles: true }));
    var state = configState[provider];
    if (state) {
      baseUrl.value = state.baseUrl;
      apiKey.value = state.apiKey;
      if (step2Form && step2Form.querySelector("option")) step2Form.hidden = false;
    } else if (apiKey) {
      apiKey.value = "";
    }
    if (step2Provider) step2Provider.value = provider;
    suppressDirty = false;
  }

  function moveSharedConfig(provider) {
    var slot = panel.querySelector('[data-ai-config-slot="' + provider + '"]');
    if (sharedConfig && slot) slot.appendChild(sharedConfig);
  }

  function updateConfigCopy(provider) {
    var title = panel.querySelector("#router-config-title");
    var description = panel.querySelector("#router-config-description");
    var copy = {
      openai: ["API config · Codex", "Kết nối API OpenAI cho các luồng AI dùng chung."],
      anthropic: ["API config · Claude", "Kết nối Anthropic API cho các luồng AI dùng chung."],
      "9router": ["API config · 9Router", "Cấu hình proxy 9Router tự triển khai và xác nhận endpoint."],
      openrouter: ["API config · OpenRouter", "Kết nối OpenRouter API và chọn model muốn sử dụng."]
    }[provider] || ["API config", "Cấu hình endpoint và xác nhận kết nối với dịch vụ AI đang chọn."];
    if (title) title.textContent = copy[0];
    if (description) description.textContent = copy[1];
  }

  function updateConfigurationBadges() {
    tabs.forEach(function (tab) {
      var badge = tab.querySelector(".ai-service-tab-status");
      if (!badge) return;
      var provider = tab.getAttribute("data-ai-tab");
      var account = accountState[provider];
      var routerUsing = provider === routerConfiguredProvider;
      var isUsing = !!(account && account.enabled) || routerUsing;
      var isConfigured = isUsing || !!(account && account.configured);
      var status = isUsing ? "ĐANG DÙNG" : (isConfigured ? "KHÔNG DÙNG" : "CHƯA CẤU HÌNH");
      badge.textContent = status;
      badge.classList.toggle("is-configured", isUsing);
      badge.classList.toggle("is-not-using", !isUsing && isConfigured);
      badge.classList.toggle("is-unconfigured", !isUsing && !isConfigured);
      badge.hidden = false;
    });
  }

  window.addEventListener("ceph-ai-account-state", function (event) {
    var detail = event.detail || {};
    if (detail.provider !== "openai" && detail.provider !== "anthropic") return;
    accountState[detail.provider].enabled = !!detail.enabled;
    if (detail.configured) {
      accountState[detail.provider].configured = true;
      rememberAccountConfigured(detail.provider);
    }
    updateConfigurationBadges();
  });

  function selectTab(provider) {
    if (!provider || provider === activeProvider) return;
    remember(activeProvider);
    tabs.forEach(function (tab) {
      var selected = tab.getAttribute("data-ai-tab") === provider;
      tab.classList.toggle("is-active", selected);
      tab.setAttribute("aria-selected", selected ? "true" : "false");
    });
    tabPanels.forEach(function (tabPanel) {
      tabPanel.hidden = tabPanel.getAttribute("data-ai-tab-panel") !== provider;
    });
    setProvider(provider);
    moveSharedConfig(provider);
    updateConfigCopy(provider);
    if (dirty[activeProvider]) {
      warning.hidden = false;
      warning.textContent = "⚠ Có thay đổi chưa lưu trong tab " + activeProvider + ".";
    } else {
      warning.hidden = true;
    }
    activeProvider = provider;
  }

  tabs.forEach(function (tab) {
    tab.addEventListener("click", function () { selectTab(tab.getAttribute("data-ai-tab")); });
  });
  panel.addEventListener("input", function (event) {
    if (suppressDirty || !event.target.matches("input:not([type=radio]), textarea")) return;
    dirty[activeProvider] = true;
  });
  panel.addEventListener("change", function (event) {
    if (suppressDirty || event.target.matches('input[name="router_provider"]')) return;
    if (event.target.matches("select, input:not([type=radio]), textarea")) dirty[activeProvider] = true;
  });
  moveSharedConfig(activeProvider);
  if (step2Provider) step2Provider.value = activeProvider;
  updateConfigCopy(activeProvider);
  updateConfigurationBadges();
})();
