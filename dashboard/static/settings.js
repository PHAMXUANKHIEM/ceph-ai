(function () {
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
  var modelSelect = document.getElementById("router-model-select");
  var modelFilter = document.getElementById("router-model-filter");
  var connectedView = document.getElementById("router-connected-view");
  var wizardView = document.getElementById("router-wizard-view");
  var changeModelBtn = document.getElementById("router-change-model-btn");

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

    verifyBtn.disabled = true;
    resultEl.hidden = true;
    resultEl.classList.remove("ai-test-ok", "ai-test-fail");
    resultEl.hidden = false;
    resultEl.textContent = "Đang kết nối 9router...";

    var body = new URLSearchParams();
    body.set("router_api_key", apiKey);
    body.set("router_base_url", baseUrl);

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
        showResult(false, "Không thể kết nối " + (baseUrl || "9router") + " — kiểm tra host/port");
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
})();
