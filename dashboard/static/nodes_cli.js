(function () {
  var panel = document.getElementById("cli-panel");
  if (!panel) {
    return; // /nodes with no host selected, or not on this page at all
  }

  var host = panel.dataset.host;
  var buttonsEl = document.getElementById("cli-buttons");
  var outputEl = document.getElementById("cli-output");
  var outputTitleEl = document.getElementById("cli-output-title");
  var outputMetaEl = document.getElementById("cli-output-meta");
  var outputBodyEl = document.getElementById("cli-output-body");
  var historyBodyEl = document.getElementById("cli-history-body");

  // Always renders in Asia/Ho_Chi_Minh regardless of the viewing browser's
  // own OS timezone — see dashboard/static/chat_widget.js's identical
  // helper for why (backend now sends an explicit UTC "Z" suffix,
  // dashboard/vntime.py::to_utc_iso).
  function _vnParts(d) {
    var parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Ho_Chi_Minh",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false,
    }).formatToParts(d);
    var map = {};
    parts.forEach(function (p) { map[p.type] = p.value; });
    return map;
  }
  function formatTimestamp(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    var p = _vnParts(d);
    return p.year + "-" + p.month + "-" + p.day + " " + p.hour + ":" + p.minute + ":" + p.second;
  }

  function apiUrl() {
    return "/api/nodes/" + encodeURIComponent(host) + "/diagnostics";
  }

  function handleAuthRedirect(response) {
    if (response.redirected && response.url.indexOf("/login") !== -1) {
      window.location.reload();
      throw new Error("unauthenticated");
    }
    return response;
  }

  function renderHistory(runs) {
    while (historyBodyEl.firstChild) historyBodyEl.removeChild(historyBodyEl.firstChild);
    if (!runs || !runs.length) {
      var emptyRow = document.createElement("tr");
      emptyRow.className = "empty-row";
      emptyRow.innerHTML = '<td colspan="4" class="empty-state">Chưa có lịch sử.</td>';
      historyBodyEl.appendChild(emptyRow);
      return;
    }
    runs.forEach(function (run) {
      var row = document.createElement("tr");
      var resultLabel = run.success ? "OK" : "Lỗi";
      var resultClass = run.success ? "cli-result-ok" : "cli-result-error";
      row.innerHTML =
        "<td>" + formatTimestamp(run.created_at) + "</td>" +
        "<td><code></code></td>" +
        "<td></td>" +
        '<td><span class="' + resultClass + '"></span></td>';
      row.querySelector("code").textContent = run.command_label;
      row.children[2].textContent = run.actor;
      row.querySelector("." + resultClass).textContent = resultLabel;
      row.style.cursor = "pointer";
      row.addEventListener("click", function () { showOutput(run); });
      historyBodyEl.appendChild(row);
    });
  }

  function showOutput(run) {
    outputEl.hidden = false;
    outputTitleEl.textContent = run.command_label;
    outputMetaEl.textContent =
      run.host + " · " + run.actor + " · " + formatTimestamp(run.created_at) +
      (run.success ? "" : " · lỗi");
    outputBodyEl.textContent = run.output_excerpt;
    outputBodyEl.classList.toggle("cli-output-error", !run.success);
  }

  function renderButtons(commands) {
    while (buttonsEl.firstChild) buttonsEl.removeChild(buttonsEl.firstChild);
    commands.forEach(function (cmd) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-sm cli-cmd-btn";
      btn.textContent = cmd.label;
      btn.addEventListener("click", function () { runCommand(cmd.command_id, btn); });
      buttonsEl.appendChild(btn);
    });
  }

  function setButtonsDisabled(disabled) {
    buttonsEl.querySelectorAll("button").forEach(function (b) { b.disabled = disabled; });
  }

  function loadHistory() {
    fetch(apiUrl(), { credentials: "same-origin" })
      .then(handleAuthRedirect)
      .then(function (response) { return response.json(); })
      .then(function (data) {
        renderButtons(data.commands || []);
        renderHistory(data.runs || []);
      })
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
      });
  }

  function runCommand(commandId, btn) {
    setButtonsDisabled(true);
    if (btn) btn.classList.add("is-running");
    var body = new URLSearchParams();
    body.set("command_id", commandId);

    fetch(apiUrl(), { method: "POST", credentials: "same-origin", body: body })
      .then(handleAuthRedirect)
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (data) {
            throw new Error(data.detail || "HTTP " + response.status);
          });
        }
        return response.json();
      })
      .then(function (run) {
        showOutput(run);
        loadHistory();
      })
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
        outputEl.hidden = false;
        outputTitleEl.textContent = "Lỗi";
        outputMetaEl.textContent = "";
        outputBodyEl.textContent = err.message;
        outputBodyEl.classList.add("cli-output-error");
      })
      .finally(function () {
        setButtonsDisabled(false);
        if (btn) btn.classList.remove("is-running");
      });
  }

  loadHistory();
})();
