(function () {
  var panel = document.getElementById("volumes-panel");
  if (!panel) {
    return; // /volumes with no pool selected, or not on this page at all
  }

  var pool = panel.dataset.pool;
  // VolumeMetric is written once per Watcher poll (see that model's own
  // docstring) — polling this app's own history API faster than that
  // cadence would just re-fetch the identical rows repeatedly. 15s matches
  // the Watcher's own default poll interval closely enough without being
  // tied to its exact configured value.
  var REFRESH_INTERVAL_MS = 15000;

  var searchForm = document.getElementById("volume-search-form");
  var searchInput = document.getElementById("volume-search-input");
  var datalist = document.getElementById("volume-datalist");
  var suggestionsEl = document.getElementById("volume-suggestions");
  var emptyState = document.getElementById("volume-chart-empty");
  var chartStack = document.getElementById("volume-chart-stack");
  var spinner = document.getElementById("header-spinner");
  var errorFooter = document.getElementById("volumes-error-footer");
  var errorTimestamp = document.getElementById("volumes-error-timestamp");
  var retryBtn = document.getElementById("volumes-retry-btn");

  var GRID_COLOR = "#1e293b";
  var CROSSHAIR_COLOR = "#334155";
  var PEAK_COLOR = "#f472b6";

  var METRICS = [
    { key: "iops", name: "IOPS", unit: "ops/s", field: "iops", color: "#4ade80" },
    { key: "read_latency_ms", name: "Read Latency", unit: "ms", field: "read_latency_ms", color: "#38bdf8" },
    { key: "write_latency_ms", name: "Write Latency", unit: "ms", field: "write_latency_ms", color: "#fb923c" }
  ];

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
  function pad2(n) { return String(n).padStart(2, "0"); }
  function formatClock(date) {
    return pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds());
  }
  function formatDateTime(date) {
    return pad2(date.getDate()) + "/" + pad2(date.getMonth() + 1) + " " + pad2(date.getHours()) + ":" + pad2(date.getMinutes());
  }

  function niceMax(value) {
    if (value <= 0) return 1;
    var exp = Math.floor(Math.log10(value));
    var base = Math.pow(10, exp);
    var frac = value / base;
    var niceFrac = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 2.5 ? 2.5 : frac <= 5 ? 5 : 10;
    return niceFrac * base;
  }

  function formatValue(cfg, v) {
    if (v == null) return "—";
    return cfg.unit === "ms" ? v.toFixed(2) : v.toFixed(1);
  }

  /* ---------- build DOM for the 3 stacked metric sections (once) ---------- */
  var sections = {};
  METRICS.forEach(function (cfg) {
    var section = document.createElement("div");
    section.className = "metric-section";
    section.dataset.metric = cfg.key;

    section.innerHTML =
      '<div class="metric-section-head">' +
        '<span class="metric-name" style="color:' + cfg.color + '">' + cfg.name + "</span>" +
        '<span class="metric-value"><span class="value">—</span><span class="unit">' + cfg.unit + "</span></span>" +
        '<span class="metric-sub" data-role="peak">Đỉnh: —</span>' +
      "</div>" +
      '<div class="metric-chart-wrap">' +
        "<canvas></canvas>" +
        '<div class="chart-empty-overlay" hidden>Chưa có dữ liệu</div>' +
        '<div class="tt" hidden><div class="tt-time"></div><div class="tt-rows"></div></div>' +
      "</div>";
    chartStack.appendChild(section);

    var canvas = section.querySelector("canvas");
    sections[cfg.key] = {
      cfg: cfg,
      canvas: canvas,
      ctx: canvas.getContext("2d"),
      wrap: section.querySelector(".metric-chart-wrap"),
      emptyOverlay: section.querySelector(".chart-empty-overlay"),
      tooltip: section.querySelector(".tt"),
      nameEl: section.querySelector(".metric-name"),
      valueEl: section.querySelector(".value"),
      peakEl: section.querySelector('[data-role="peak"]'),
      geom: null,
      hasDrawnOnce: false
    };
    bindChartHover(sections[cfg.key]);
  });

  function bindChartHover(section) {
    section.canvas.addEventListener("pointermove", function (e) {
      var g = section.geom;
      if (!g || g.n <= 0) return;
      var rect = section.canvas.getBoundingClientRect();
      var px = e.clientX - rect.left;
      var rel = (px - g.padLeft) / g.plotW;
      var idx = clamp(Math.round(rel * (g.n - 1)), 0, g.n - 1);
      App.hoverIndex = idx;
      App.hoverSection = section.cfg.key;
      drawAllCharts();
    });
    section.canvas.addEventListener("pointerleave", function () {
      App.hoverIndex = null;
      App.hoverSection = null;
      drawAllCharts();
    });
  }

  /* ---------- App state ---------- */
  var App = {
    currentImage: null,
    knownImages: [],
    timestamps: [],
    buffers: {}, // field -> [values]
    peak: {},
    saturatedNow: false,
    hoverIndex: null,
    hoverSection: null,
    pollTimer: null,
    lastErrorAt: null
  };

  function selectImage(image) {
    if (App.pollTimer) { clearInterval(App.pollTimer); App.pollTimer = null; }
    App.currentImage = image;
    App.hoverIndex = null;
    App.hoverSection = null;
    emptyState.hidden = true;
    chartStack.hidden = false;
    METRICS.forEach(function (cfg) { sections[cfg.key].hasDrawnOnce = false; });
    renderSuggestions(searchInput.value);
    fetchHistory();
    // Don't poll the selected image's history while the tab is hidden.
    App.pollTimer = setInterval(function () { if (document.hidden) return; fetchHistory(); }, REFRESH_INTERVAL_MS);
  }

  // 2026-07-29: the search box's <datalist> alone turned out to be too
  // easy to miss (its suggestions only show up once the operator clicks
  // into the field, and on some browsers only after typing a first
  // character) — this app already persists every volume name it has ever
  // seen (VolumeMetric), so there's no reason to hide that list behind
  // typing. Renders every known name as a clickable chip, live-filtered
  // by whatever's currently in the search box; the box itself still works
  // for typing an exact name directly (e.g. one not seen yet).
  function renderSuggestions(filterText) {
    if (!suggestionsEl) return;
    var q = (filterText || "").trim().toLowerCase();
    suggestionsEl.innerHTML = "";
    if (!App.knownImages.length) {
      var hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = "Chưa có Volume nào được ghi nhận trong pool này.";
      suggestionsEl.appendChild(hint);
      return;
    }
    var matches = App.knownImages.filter(function (name) {
      return !q || name.toLowerCase().indexOf(q) !== -1;
    });
    if (!matches.length) {
      var none = document.createElement("span");
      none.className = "hint";
      none.textContent = "Không khớp tên nào.";
      suggestionsEl.appendChild(none);
      return;
    }
    matches.forEach(function (name) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "btn btn-sm " + (name === App.currentImage ? "btn-primary" : "btn-ghost");
      btn.textContent = name;
      btn.addEventListener("click", function () {
        searchInput.value = name;
        selectImage(name);
      });
      suggestionsEl.appendChild(btn);
    });
  }

  function fetchHistory() {
    if (!App.currentImage) return;
    if (spinner) spinner.hidden = false;
    fetch(
      "/api/volumes/" + encodeURIComponent(pool) + "/" + encodeURIComponent(App.currentImage) + "/history",
      { credentials: "same-origin" }
    )
      .then(function (response) {
        // require_login raises a 303 to /login on an expired session —
        // fetch's default redirect mode resolves that transparently,
        // landing here as a 200 whose *final* URL is /login.
        if (response.redirected && response.url.indexOf("/login") !== -1) {
          window.location.reload();
          throw new Error("unauthenticated");
        }
        if (!response.ok) {
          return response.json().then(function (body) {
            throw new Error(body.detail || "HTTP " + response.status);
          });
        }
        return response.json();
      })
      .then(onSuccess)
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
        onError(err.message);
      });
  }

  function onSuccess(data) {
    if (spinner) spinner.hidden = true;
    setErrorUI(false);

    App.timestamps = data.samples.map(function (s) { return new Date(s.polled_at); });
    METRICS.forEach(function (cfg) {
      App.buffers[cfg.field] = data.samples.map(function (s) { return s[cfg.field]; });
    });
    App.peak = data.peak || {};
    App.saturatedNow = !!data.saturated;

    drawAllCharts(true);
  }

  function onError(message) {
    App.lastErrorAt = new Date();
    if (spinner) spinner.hidden = true;
    setErrorUI(true, message);
  }

  function setErrorUI(isError, message) {
    if (errorFooter) errorFooter.hidden = !isError;
    if (isError && errorTimestamp) {
      errorTimestamp.textContent = "Lần thử cuối thất bại lúc " + formatClock(App.lastErrorAt) + " — " + message;
    }
  }

  if (retryBtn) retryBtn.addEventListener("click", fetchHistory);

  if (searchForm) {
    searchForm.addEventListener("submit", function (e) {
      e.preventDefault();
      var value = (searchInput.value || "").trim();
      if (!value) return;
      selectImage(value);
    });
  }

  if (searchInput) {
    searchInput.addEventListener("input", function () { renderSuggestions(searchInput.value); });
  }

  function loadKnownImages() {
    fetch("/api/volumes/" + encodeURIComponent(pool) + "/images", { credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data) return;
        App.knownImages = data.images || [];
        renderSuggestions(searchInput.value);
        if (datalist) {
          datalist.innerHTML = "";
          App.knownImages.forEach(function (image) {
            var option = document.createElement("option");
            option.value = image;
            datalist.appendChild(option);
          });
        }
      })
      .catch(function () { /* suggestions are a convenience, not required */ });
  }

  /* ---------- drawing ---------- */
  function drawAllCharts(firstPaint) {
    METRICS.forEach(function (cfg) { drawSection(sections[cfg.key], firstPaint); });
  }

  function drawSection(section, firstPaint) {
    var cfg = section.cfg;
    var timestamps = App.timestamps;
    var n = timestamps.length;
    var values = App.buffers[cfg.field] || [];
    var peakEntry = App.peak[cfg.field];

    section.nameEl.textContent = cfg.name + (App.saturatedNow && cfg.key === "iops" ? " ⚠ Bão hoà" : "");
    section.nameEl.style.color = App.saturatedNow && cfg.key === "iops" ? "var(--critical)" : cfg.color;
    var lastValue = values.length ? values[values.length - 1] : null;
    section.valueEl.textContent = formatValue(cfg, lastValue);
    section.peakEl.textContent = peakEntry
      ? "Đỉnh: " + formatValue(cfg, peakEntry.value) + " " + cfg.unit + " · " + formatDateTime(new Date(peakEntry.at))
      : "Đỉnh: —";

    section.emptyOverlay.hidden = n > 0;
    section.canvas.style.visibility = "visible";

    if (firstPaint && !section.hasDrawnOnce) {
      section.canvas.classList.add("is-drawing-in");
      section.hasDrawnOnce = true;
    }

    var canvas = section.canvas, ctx = section.ctx;
    var rect = canvas.getBoundingClientRect();
    var dpr = window.devicePixelRatio || 1;
    var w = Math.max(rect.width, 40), h = Math.max(rect.height, 40);
    var pxW = Math.round(w * dpr), pxH = Math.round(h * dpr);
    if (canvas.width !== pxW || canvas.height !== pxH) { canvas.width = pxW; canvas.height = pxH; }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);

    if (n === 0) { section.geom = null; return; }

    var padLeft = 40, padRight = 8, padTop = 8, padBottom = 18;
    var plotW = Math.max(w - padLeft - padRight, 10);
    var plotH = Math.max(h - padTop - padBottom, 10);

    var dataMax = 0;
    values.forEach(function (v) { if (v != null && v > dataMax) dataMax = v; });
    var peakVal = peakEntry ? peakEntry.value : 0;
    var yMax = niceMax(Math.max(dataMax, peakVal) * 1.2 || 1);

    function xAt(i) { return padLeft + (n <= 1 ? 0 : (i / (n - 1)) * plotW); }
    function yAt(v) { return padTop + plotH - (clamp(v, 0, yMax) / (yMax || 1)) * plotH; }
    section.geom = { padLeft: padLeft, plotW: plotW, plotH: plotH, padTop: padTop, n: n, xAt: xAt, yAt: yAt };

    // gridlines
    ctx.strokeStyle = GRID_COLOR;
    ctx.lineWidth = 1;
    ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
    ctx.fillStyle = "#64748b";
    ctx.textBaseline = "middle";
    [0, yMax].forEach(function (v) {
      var y = Math.round(yAt(v)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padLeft, y);
      ctx.lineTo(padLeft + plotW, y);
      ctx.stroke();
      ctx.textAlign = "right";
      ctx.fillText(v >= 10 ? v.toFixed(0) : v.toFixed(1), padLeft - 5, y);
    });

    // x-axis: date+time at start / middle / end
    ctx.textBaseline = "top";
    var tickIdxs = [];
    [0, Math.floor((n - 1) / 2), n - 1].forEach(function (idx) {
      if (idx >= 0 && tickIdxs.indexOf(idx) === -1) tickIdxs.push(idx);
    });
    tickIdxs.forEach(function (idx, i) {
      if (!timestamps[idx]) return;
      ctx.textAlign = i === 0 ? "left" : i === tickIdxs.length - 1 ? "right" : "center";
      ctx.fillText(formatClock(timestamps[idx]), xAt(idx), padTop + plotH + 4);
    });

    // peak reference line — the whole point of this chart per the operator's
    // own request: the historical best this volume has ever done, not just
    // whatever's in the currently-plotted window.
    if (peakEntry != null) {
      var yPeak = Math.round(yAt(peakEntry.value)) + 0.5;
      ctx.save();
      ctx.strokeStyle = PEAK_COLOR;
      ctx.setLineDash([4, 3]);
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(padLeft, yPeak);
      ctx.lineTo(padLeft + plotW, yPeak);
      ctx.stroke();
      ctx.restore();
    }

    var hasAnyValue = values.some(function (v) { return v != null; });
    if (!hasAnyValue) return;

    drawLine(ctx, values, xAt, yAt, cfg.color);

    // crosshair + tooltip
    if (App.hoverIndex != null && App.hoverIndex < n) {
      var xH = xAt(App.hoverIndex);
      ctx.save();
      ctx.strokeStyle = CROSSHAIR_COLOR;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(xH, padTop);
      ctx.lineTo(xH, padTop + plotH);
      ctx.stroke();
      ctx.restore();
      var hv = values[App.hoverIndex];
      if (hv != null) {
        var y = yAt(hv);
        ctx.beginPath();
        ctx.arc(xH, y, 3, 0, Math.PI * 2);
        ctx.fillStyle = cfg.color;
        ctx.fill();
      }
      if (section.cfg.key === App.hoverSection) showTooltip(section, App.hoverIndex);
      else section.tooltip.hidden = true;
    } else {
      section.tooltip.hidden = true;
    }
  }

  function drawLine(ctx, values, xAt, yAt, color) {
    var started = false;
    ctx.beginPath();
    for (var i = 0; i < values.length; i++) {
      if (values[i] == null) { started = false; continue; }
      var x = xAt(i), y = yAt(values[i]);
      if (!started) { ctx.moveTo(x, y); started = true; }
      else ctx.lineTo(x, y);
    }
    ctx.strokeStyle = color;
    ctx.lineWidth = 2;
    ctx.lineJoin = "round";
    ctx.lineCap = "round";
    ctx.stroke();
  }

  function showTooltip(section, idx) {
    var g = section.geom;
    var tt = section.tooltip;
    var ts = App.timestamps[idx];
    if (!ts) { tt.hidden = true; return; }
    tt.hidden = false;
    tt.querySelector(".tt-time").textContent = formatDateTime(ts);
    var rowsEl = tt.querySelector(".tt-rows");
    while (rowsEl.firstChild) rowsEl.removeChild(rowsEl.firstChild);
    var v = (App.buffers[section.cfg.field] || [])[idx];
    var row = document.createElement("div");
    row.className = "tt-row";
    var key = document.createElement("span"); key.className = "tt-key"; key.style.background = section.cfg.color;
    var name = document.createElement("span"); name.className = "tt-name"; name.textContent = section.cfg.name;
    var val = document.createElement("span"); val.className = "tt-value";
    val.textContent = v == null ? "—" : formatValue(section.cfg, v) + " " + section.cfg.unit;
    row.appendChild(key); row.appendChild(name); row.appendChild(val);
    rowsEl.appendChild(row);
    var wrapWidth = section.wrap.clientWidth;
    var x = g.xAt(idx);
    var translate = x < 84 ? "0" : x > wrapWidth - 84 ? "-100%" : "-50%";
    tt.style.left = x + "px";
    tt.style.transform = "translateX(" + translate + ")";
  }

  loadKnownImages();
  window.addEventListener("resize", function () { if (App.currentImage) drawAllCharts(); });
})();
