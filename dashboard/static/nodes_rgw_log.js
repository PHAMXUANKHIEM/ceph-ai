(function () {
  var panel = document.getElementById("rgw-log-panel");
  if (!panel) {
    return; // no host selected, selected node isn't RGW, or not on this page
  }

  var host = panel.dataset.host;
  var bodyEl = document.getElementById("rgw-log-body");
  var filterInputEl = document.getElementById("rgw-log-filter");
  var filterClearBtn = document.getElementById("rgw-log-filter-clear");
  var liveToggleBtn = document.getElementById("rgw-log-live-toggle");
  var statusEl = document.getElementById("rgw-log-status");
  var statusTextEl = document.getElementById("rgw-log-status-text");

  var POLL_INTERVAL_MS = 3000;
  var FILTER_DEBOUNCE_MS = 400;

  var isLive = true;
  var pollTimer = null;
  var filterDebounceTimer = null;
  var inFlight = false;

  // Always renders in Asia/Ho_Chi_Minh regardless of the viewing browser's
  // own OS timezone — see dashboard/static/chat_widget.js's identical
  // helper for why.
  function formatClock(d) {
    var parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Ho_Chi_Minh",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false,
    }).formatToParts(d);
    var map = {};
    parts.forEach(function (p) { map[p.type] = p.value; });
    return map.hour + ":" + map.minute + ":" + map.second;
  }

  function handleAuthRedirect(response) {
    if (response.redirected && response.url.indexOf("/login") !== -1) {
      window.location.reload();
      throw new Error("unauthenticated");
    }
    return response;
  }

  // Renders one line, wrapping every case-insensitive match of `term` in a
  // <mark> — built with createElement/textContent throughout (never
  // innerHTML on log content), since these lines are real radosgw log
  // output read over SSH and must be treated as untrusted text, same
  // posture as dashboard/static/chat_widget.js's buildMessage().
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

  function apiUrl(term) {
    var url = "/api/nodes/" + encodeURIComponent(host) + "/rgw-log";
    if (term) url += "?filter=" + encodeURIComponent(term);
    return url;
  }

  function load() {
    if (inFlight) return; // never overlap two in-flight requests (a slow SSH round trip + the 3s poll could otherwise stack up)
    var term = filterInputEl.value.trim();
    inFlight = true;
    if (bodyEl.childElementCount === 0) setStatus("loading", "Đang tải log " + host + "...");

    fetch(apiUrl(term), { credentials: "same-origin" })
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
        setStatus(isLive ? "live" : "paused", "Cập nhật lúc " + formatClock(new Date()));
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

  filterInputEl.addEventListener("input", function () {
    if (filterDebounceTimer) clearTimeout(filterDebounceTimer);
    filterDebounceTimer = setTimeout(function () {
      load();
      if (isLive) scheduleNextPoll(); // restart the interval from this fetch, not a stale one
    }, FILTER_DEBOUNCE_MS);
  });

  filterClearBtn.addEventListener("click", function () {
    filterInputEl.value = "";
    load();
    if (isLive) scheduleNextPoll();
  });

  setLive(true);
})();
