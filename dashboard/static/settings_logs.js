(function () {
  var panel = document.querySelector('.settings-panel[data-panel="server-log"]');
  if (!panel) {
    return; // not an admin account — panel isn't rendered at all
  }

  var nameSelect = document.getElementById("server-log-name");
  var bodyEl = document.getElementById("server-log-body");
  var filterInputEl = document.getElementById("server-log-filter");
  var filterClearBtn = document.getElementById("server-log-filter-clear");
  var liveToggleBtn = document.getElementById("server-log-live-toggle");
  var statusEl = document.getElementById("server-log-status");
  var statusTextEl = document.getElementById("server-log-status-text");

  var POLL_INTERVAL_MS = 3000;
  var FILTER_DEBOUNCE_MS = 400;

  var isLive = true;
  var pollTimer = null;
  var filterDebounceTimer = null;
  var inFlight = false;

  function handleAuthRedirect(response) {
    if (response.redirected && response.url.indexOf("/login") !== -1) {
      window.location.reload();
      throw new Error("unauthenticated");
    }
    return response;
  }

  // Same "never innerHTML on log content" posture as
  // dashboard/static/nodes_rgw_log.js::renderLine — this is real process
  // log output (exception messages, SSH command lines), untrusted text.
  function renderLine(text, term) {
    var line = document.createElement("div");
    line.className = "rgw-log-line";
    if (!term) {
      line.textContent = text;
      return line;
    }
    var lower = text.toLowerCase();
    var termLower = term.toLowerCase();
    var i = 0;
    while (i < text.length) {
      var matchAt = lower.indexOf(termLower, i);
      if (matchAt === -1) {
        line.appendChild(document.createTextNode(text.slice(i)));
        break;
      }
      if (matchAt > i) line.appendChild(document.createTextNode(text.slice(i, matchAt)));
      var mark = document.createElement("mark");
      mark.textContent = text.slice(matchAt, matchAt + term.length);
      line.appendChild(mark);
      i = matchAt + term.length;
    }
    return line;
  }

  function renderLines(lines, term) {
    while (bodyEl.firstChild) bodyEl.removeChild(bodyEl.firstChild);
    if (!lines.length) {
      var empty = document.createElement("span");
      empty.className = "rgw-log-empty";
      empty.textContent = term ? "Không có dòng nào khớp với bộ lọc." : "Log trống.";
      bodyEl.appendChild(empty);
      return;
    }
    lines.forEach(function (text) { bodyEl.appendChild(renderLine(text, term)); });
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }

  function setStatus(state, message) {
    // state: "loading" | "live" | "paused" | "error"
    statusEl.classList.toggle("is-loading", state === "loading");
    statusEl.classList.toggle("is-stale", state === "error");
    statusTextEl.textContent = message;
  }

  function apiUrl(name, term) {
    var url = "/api/settings/server-log?name=" + encodeURIComponent(name);
    if (term) url += "&filter=" + encodeURIComponent(term);
    return url;
  }

  function load() {
    if (inFlight) return; // never overlap two in-flight requests
    var name = nameSelect.value;
    var term = filterInputEl.value.trim();
    inFlight = true;
    if (bodyEl.childElementCount === 0) setStatus("loading", "Đang tải log " + name + "...");

    fetch(apiUrl(name, term), { credentials: "same-origin" })
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
        renderLines(data.lines || [], term);
        setStatus(isLive ? "live" : "paused", "Cập nhật lúc " + new Date().toLocaleTimeString("vi-VN"));
      })
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
        setStatus("error", "Lỗi: " + err.message);
      })
      .finally(function () {
        inFlight = false;
      });
  }

  function scheduleNextPoll() {
    if (pollTimer) clearTimeout(pollTimer);
    if (!isLive) return;
    pollTimer = setTimeout(function () {
      load();
      scheduleNextPoll();
    }, POLL_INTERVAL_MS);
  }

  function setLive(live) {
    isLive = live;
    liveToggleBtn.textContent = live ? "Tạm dừng" : "Theo dõi trực tiếp";
    liveToggleBtn.classList.toggle("is-active", live);
    if (live) {
      load();
      scheduleNextPoll();
    } else if (pollTimer) {
      clearTimeout(pollTimer);
      pollTimer = null;
      setStatus("paused", "Đã tạm dừng");
    }
  }

  liveToggleBtn.addEventListener("click", function () { setLive(!isLive); });

  nameSelect.addEventListener("change", function () {
    load();
    if (isLive) scheduleNextPoll(); // restart the interval from this fetch, not a stale one
  });

  filterInputEl.addEventListener("input", function () {
    if (filterDebounceTimer) clearTimeout(filterDebounceTimer);
    filterDebounceTimer = setTimeout(function () {
      load();
      if (isLive) scheduleNextPoll();
    }, FILTER_DEBOUNCE_MS);
  });

  filterClearBtn.addEventListener("click", function () {
    filterInputEl.value = "";
    load();
    if (isLive) scheduleNextPoll();
  });

  // The panel itself may start `hidden` (a different settings-nav-item is
  // active on load) — only start polling once it's actually visible, same
  // "don't burn a poll loop on something the operator isn't looking at"
  // posture as dashboard/static/volumes.js's own pool-selection guard.
  // settings.js's tab-switch handler only toggles `hidden`, it never fires
  // an event, so this listens on the nav button directly instead.
  var navItem = document.querySelector('.settings-nav-item[data-section="server-log"]');
  var started = false;
  function startOnce() {
    if (started) return;
    started = true;
    setLive(true);
  }
  if (!panel.hidden) {
    startOnce();
  } else if (navItem) {
    navItem.addEventListener("click", startOnce);
  }
})();
