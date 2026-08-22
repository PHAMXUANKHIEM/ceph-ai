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
      var row = document.createElement("div");
      row.className = "ai-limit-row" + (limit.remaining_percent <= 15 ? " ai-limit-low" : "");
      var label = document.createElement("span");
      label.textContent = limit.label;
      var value = document.createElement("strong");
      value.textContent = limit.remaining_percent + "% còn lại";
      var meter = document.createElement("progress");
      meter.max = 100;
      meter.value = limit.remaining_percent;
      row.appendChild(label); row.appendChild(value); row.appendChild(meter);
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

  // Kết nối Database: "Kiểm tra kết nối" is a side-effect-free SELECT 1
  // probe (no migration, no .env write — see /settings/database/test in
  // dashboard/routes/settings.py) so an operator can try a few
  // host/port/credential combos before the real "Lưu & chuyển database"
  // submit, which does migrate + restart everything.
  var dbTestBtn = document.getElementById("db-test-btn");
  if (dbTestBtn) {
    var dbHostInput = document.getElementById("db-host-input");
    var dbPortInput = document.getElementById("db-port-input");
    var dbNameInput = document.getElementById("db-name-input");
    var dbUsernameInput = document.getElementById("db-username-input");
    var dbPasswordInput = document.getElementById("db-password-input");
    var dbUrlInput = document.getElementById("db-url-input");
    var dbResultEl = document.getElementById("db-test-result");

    // Two input modes for the same underlying DATABASE_URL — "Nhập từng
    // trường" (5 separate inputs) or "Nhập Database URL" (one pasted
    // connection string, see _resolve_database_url in
    // dashboard/routes/settings.py, which prefers the raw URL whenever
    // it's non-blank). Only toggles which group is VISIBLE — the server
    // decides which one actually applies, so submitting with the "wrong"
    // group hidden-but-filled from a previous edit still behaves correctly.
    var dbModeRadios = Array.prototype.slice.call(document.querySelectorAll('input[name="db_input_mode"]'));
    var dbModeFields = Array.prototype.slice.call(document.querySelectorAll("[data-db-modes]"));
    if (dbModeRadios.length) {
      var applyDbModeVisibility = function () {
        var checked = dbModeRadios.filter(function (r) { return r.checked; })[0];
        var mode = checked ? checked.value : "fields";
        dbModeFields.forEach(function (field) {
          field.hidden = field.getAttribute("data-db-modes") !== mode;
        });
      };
      dbModeRadios.forEach(function (r) { r.addEventListener("change", applyDbModeVisibility); });
      applyDbModeVisibility();
    }

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
        if (event.defaultPrevented) {
          return; // the existing confirm() onsubmit handler already vetoed this submit
        }
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
        dialogTitle.textContent = "Restart " + service + "?";
        dialogDescription.textContent = button.getAttribute("data-restart-warning") || "This service will be briefly unavailable.";
        dialogConfirm.textContent = "Restart " + service;
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
      dialogConfirm.textContent = "Restarting...";
      pendingRestartButton.classList.add("is-loading");
      pendingRestartButton.innerHTML = "<span>↻</span> Restarting...";
      form.submit();
    });
  }
})();

// AI Action Policy: search and filters run locally so looking up an action
// never reloads the page or disturbs a partially completed policy form.
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
  if (!table || !search || !classification || !source || !reset || !result || !empty ||
      !pagination || !previous || !next || !pageStatus) return;

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
  function render() {
    var query = normalize(search.value);
    var wantedClassification = classification.value;
    var wantedSource = source.value;
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
    empty.hidden = filtered.length !== 0;
    if (tableWrap) tableWrap.hidden = filtered.length === 0;
    pagination.hidden = filtered.length === 0;
    pageStatus.textContent = "Trang " + currentPage + " / " + pageCount;
    previous.disabled = currentPage <= 1;
    next.disabled = currentPage >= pageCount;
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
    classification.value = "";
    source.value = "";
    resetPageAndRender();
    search.focus();
  });
  render();
})();

// Policy changes use one short, consistent confirmation for both SAFE and
// RISKY. The server validates it again; this prompt is only the UI layer.
(function () {
  var forms = document.querySelectorAll(".action-policy-change-form");
  Array.prototype.forEach.call(forms, function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var confirmation = window.prompt("Nhập OK để áp dụng policy");
      if (confirmation !== "OK") return;
      var field = form.querySelector('input[name="confirmation"]');
      if (!field) return;
      field.value = confirmation;
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
