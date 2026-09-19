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
  var combobox = document.getElementById("volume-combobox");
  var dropdown = document.getElementById("volume-dropdown");
  var dropdownOptions = document.getElementById("volume-dropdown-options");
  var dropdownToggle = document.getElementById("volume-combobox-toggle");
  var clearBtn = document.getElementById("volume-clear-btn");
  var selectedVolumeEl = document.getElementById("volume-selected-volume");
  var selectedNameEl = document.getElementById("volume-selected-name");
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

  var pickerState = { loaded: false, filtered: [], highlighted: -1, virtualRowHeight: 64 };

  function formatBytes(bytes) {
    var value = Number(bytes || 0);
    if (!value) return "—";
    var units = ["B", "KiB", "MiB", "GiB", "TiB"];
    var index = 0;
    while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
    return (value >= 100 || index === 0 ? value.toFixed(0) : value.toFixed(1)) + " " + units[index];
  }

  function shortVolumeName(value) {
    value = String(value || "");
    var prefix = value.indexOf("volume-") === 0 ? "volume-" : "";
    var body = prefix ? value.slice(prefix.length) : value;
    if (body.length <= 16) return value;
    return prefix + body.slice(0, 8) + "..." + body.slice(-4);
  }

  function volumeLabel(record) { return record.display_name || shortVolumeName(record.name); }
  function volumeIdentifier(record) { return record.image_id || record.name; }

  function renderVolumeOption(record, index) {
    var option = document.createElement("button");
    option.type = "button";
    option.className = "volume-dropdown-option";
    option.id = "volume-dropdown-option-" + index;
    option.dataset.volumeIndex = String(index);
    option.setAttribute("role", "option");
    option.setAttribute("aria-selected", record.name === App.currentImage ? "true" : "false");
    option.classList.toggle("is-highlighted", index === pickerState.highlighted);

    var name = document.createElement("strong");
    name.className = "volume-dropdown-name";
    name.textContent = volumeLabel(record);
    var meta = document.createElement("span");
    meta.className = "volume-dropdown-meta";
    var status = record.status_available === false ? "unknown" : (record.status || "inactive");
    meta.textContent = volumeIdentifier(record) + " · " + formatBytes(record.size_bytes) + " · " + status;
    option.appendChild(name);
    option.appendChild(meta);
    option.addEventListener("mouseenter", function () { setHighlighted(index, false); });
    option.addEventListener("click", function () {
      searchInput.value = record.name;
      selectImage(record.name);
    });
    return option;
  }

  function setHighlighted(index, rerender) {
    if (!pickerState.filtered.length) return;
    pickerState.highlighted = Math.max(0, Math.min(index, pickerState.filtered.length - 1));
    if (rerender) renderVolumeOptions();
    dropdownOptions.querySelectorAll("[data-volume-index]").forEach(function (option) {
      option.classList.toggle("is-highlighted", Number(option.dataset.volumeIndex) === pickerState.highlighted);
    });
    var active = document.getElementById("volume-dropdown-option-" + pickerState.highlighted);
    if (active) {
      active.scrollIntoView({ block: "nearest" });
      searchInput.setAttribute("aria-activedescendant", active.id);
    }
  }

  function renderVolumeOptions() {
    if (!dropdownOptions) return;
    dropdownOptions.innerHTML = "";
    if (!pickerState.filtered.length) {
      var empty = document.createElement("div");
      empty.className = "volume-dropdown-empty";
      empty.textContent = pickerState.loaded ? "Không tìm thấy volume nào" : "Đang tải danh sách volume…";
      if (pickerState.loaded && !App.knownImages.length) empty.textContent = "Pool này chưa có volume";
      dropdownOptions.appendChild(empty);
      dropdownOptions.classList.remove("is-virtual");
      dropdownOptions.style.height = "";
      return;
    }

    var virtual = pickerState.filtered.length > 50;
    dropdownOptions.classList.toggle("is-virtual", virtual);
    if (!virtual) {
      dropdownOptions.style.height = "";
      pickerState.filtered.forEach(function (record, index) {
        dropdownOptions.appendChild(renderVolumeOption(record, index));
      });
      return;
    }

    dropdownOptions.style.height = (pickerState.filtered.length * pickerState.virtualRowHeight) + "px";
    var start = Math.max(0, Math.floor(dropdownOptions.scrollTop / pickerState.virtualRowHeight) - 5);
    var end = Math.min(pickerState.filtered.length, start + Math.ceil(320 / pickerState.virtualRowHeight) + 10);
    for (var index = start; index < end; index += 1) {
      var option = renderVolumeOption(pickerState.filtered[index], index);
      option.style.position = "absolute";
      option.style.top = (index * pickerState.virtualRowHeight) + "px";
      option.style.left = "0";
      option.style.right = "0";
      dropdownOptions.appendChild(option);
    }
  }

  function filterVolumeOptions(query) {
    var q = (query || "").trim().toLowerCase();
    pickerState.filtered = App.knownImages.filter(function (record) {
      return !q || [record.name, record.display_name, record.image_id].some(function (value) {
        return String(value || "").toLowerCase().indexOf(q) !== -1;
      });
    });
    var selectedIndex = pickerState.filtered.findIndex(function (record) { return record.name === App.currentImage; });
    pickerState.highlighted = selectedIndex >= 0 ? selectedIndex : (pickerState.filtered.length ? 0 : -1);
    renderVolumeOptions();
  }

  function openVolumeDropdown() {
    if (!dropdown) return;
    filterVolumeOptions(searchInput.value);
    dropdown.hidden = false;
    combobox.classList.add("is-open");
    searchInput.setAttribute("aria-expanded", "true");
    dropdownToggle.setAttribute("aria-expanded", "true");
    var rect = combobox.getBoundingClientRect();
    combobox.classList.toggle("is-flipped", window.innerHeight - rect.bottom < 330 && rect.top > 330);
  }

  function closeVolumeDropdown() {
    if (!dropdown) return;
    dropdown.hidden = true;
    combobox.classList.remove("is-open", "is-flipped");
    searchInput.setAttribute("aria-expanded", "false");
    dropdownToggle.setAttribute("aria-expanded", "false");
    searchInput.removeAttribute("aria-activedescendant");
  }

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
    closeVolumeDropdown();
    App.currentImage = image;
    App.hoverIndex = null;
    App.hoverSection = null;
    // Do not show the previous volume's values while the new history request
    // is in flight. An empty chart is less misleading than stale telemetry.
    App.timestamps = [];
    App.buffers = {};
    App.peak = {};
    App.saturatedNow = false;
    emptyState.hidden = true;
    chartStack.hidden = false;
    if (clearBtn) clearBtn.hidden = false;
    if (selectedVolumeEl) selectedVolumeEl.hidden = false;
    if (selectedNameEl) selectedNameEl.textContent = image;
    METRICS.forEach(function (cfg) { sections[cfg.key].hasDrawnOnce = false; });
    drawAllCharts();
    setErrorUI(false);
    renderSuggestions(searchInput.value);
    fetchHistory();
    App.pollTimer = setInterval(fetchHistory, REFRESH_INTERVAL_MS);
  }

  function renderSuggestions(filterText) { filterVolumeOptions(filterText); }

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

    // The API may canonicalize a UUID/RBD Image ID to its RBD image name.
    // Keep that canonical value for the 15-second refresh loop so an alias
    // lookup does not trigger another live inventory lookup every cycle.
    if (data.image && data.image !== App.currentImage) {
      App.currentImage = data.image;
      if (searchInput) searchInput.value = data.image;
      if (selectedNameEl) selectedNameEl.textContent = data.image;
    }

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

  if (clearBtn) {
    clearBtn.addEventListener("click", function () {
      if (App.pollTimer) { clearInterval(App.pollTimer); App.pollTimer = null; }
      App.currentImage = null;
      App.timestamps = [];
      App.buffers = {};
      App.peak = {};
      App.saturatedNow = false;
      if (searchInput) searchInput.value = "";
      if (clearBtn) clearBtn.hidden = true;
      if (selectedVolumeEl) selectedVolumeEl.hidden = true;
      if (chartStack) chartStack.hidden = true;
      if (emptyState) emptyState.hidden = true;
      setErrorUI(false);
    });
  }

  if (searchInput) {
    searchInput.addEventListener("focus", openVolumeDropdown);
    searchInput.addEventListener("click", openVolumeDropdown);
    searchInput.addEventListener("input", function () {
      openVolumeDropdown();
      filterVolumeOptions(searchInput.value);
    });
    searchInput.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        openVolumeDropdown();
        setHighlighted(pickerState.highlighted + (event.key === "ArrowDown" ? 1 : -1), true);
      } else if (event.key === "Enter" && !dropdown.hidden && pickerState.highlighted >= 0) {
        event.preventDefault();
        var record = pickerState.filtered[pickerState.highlighted];
        searchInput.value = record.name;
        selectImage(record.name);
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeVolumeDropdown();
      }
    });
  }

  if (dropdownToggle) {
    dropdownToggle.addEventListener("mousedown", function (event) { event.preventDefault(); });
    dropdownToggle.addEventListener("click", function () {
      if (dropdown.hidden) { openVolumeDropdown(); searchInput.focus(); }
      else { closeVolumeDropdown(); }
    });
  }
  if (dropdownOptions) dropdownOptions.addEventListener("scroll", function () {
    if (pickerState.filtered.length > 50) renderVolumeOptions();
  });
  document.addEventListener("click", function (event) {
    if (combobox && !combobox.contains(event.target)) closeVolumeDropdown();
  });
  window.addEventListener("resize", function () {
    if (dropdown && !dropdown.hidden) openVolumeDropdown();
  });

  function loadKnownImages() {
    fetch("/api/volumes/" + encodeURIComponent(pool) + "/images?details=1", { credentials: "same-origin" })
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (!data) return;
        App.knownImages = (data.images || []).map(function (record) {
          return typeof record === "string" ? {name: record, display_name: null, image_id: null, size_bytes: 0, status: "inactive", status_available: false} : record;
        });
        pickerState.loaded = true;
        renderSuggestions(searchInput.value);
      })
      .catch(function () {
        pickerState.loaded = true;
        renderSuggestions(searchInput.value);
      });
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
    // A one-sample history has no line segment, so a normal stroked path is
    // mathematically invisible. Keep the chart honest while making that
    // valid (and common after a fresh Watcher start) sample visible.
    if (n === 1 && values[0] != null) {
      ctx.beginPath();
      ctx.arc(xAt(0), yAt(values[0]), 3, 0, Math.PI * 2);
      ctx.fillStyle = cfg.color;
      ctx.fill();
    }

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
