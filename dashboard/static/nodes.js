(function () {
  var stack = document.getElementById("metrics-stack");
  if (!stack) {
    return; // /nodes with no host selected, or not on this page at all
  }

  var host = stack.dataset.host;
  var POLL_INTERVAL_MS = 3000;
  // Real cadence is one SSH round trip per poll (~3s), not the 1s a mock
  // dashboard could fake — "last 2 minutes" at that cadence is 40 points.
  var WINDOW_SECONDS = 120;
  var MAX_POINTS = Math.round((WINDOW_SECONDS * 1000) / POLL_INTERVAL_MS);

  var TOOLTIP_BG = "#0f172a";
  var GRID_COLOR = "#1e293b";
  var CROSSHAIR_COLOR = "#334155";

  var METRICS = [
    {
      key: "cpu_percent", name: "CPU", unit: "%", fixedMax: 100,
      series: [{ field: "cpu_percent", color: "#4ade80" }]
    },
    {
      key: "mem_percent", name: "RAM", unit: "%", fixedMax: 100,
      series: [{ field: "mem_percent", color: "#38bdf8" }]
    },
    {
      key: "disk_iops", name: "Disk IOPS", unit: "ops/s",
      series: [
        { field: "disk_read_iops", label: "read", color: "#4ade80" },
        { field: "disk_write_iops", label: "write", color: "#fb923c" }
      ]
    },
    {
      key: "disk_latency_ms", name: "Disk Latency", unit: "ms",
      series: [{ field: "disk_latency_ms", color: "#fbbf24" }]
    }
  ];

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function pad2(n) { return String(n).padStart(2, "0"); }
  function formatClock(date) {
    return pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds());
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
    if (cfg.unit === "%") return v.toFixed(1);
    if (cfg.unit === "ms") return v.toFixed(2);
    return v.toFixed(2);
  }

  /* ---------- build DOM for the 4 stacked metric sections ---------- */
  var sections = {};
  METRICS.forEach(function (cfg) {
    var section = document.createElement("div");
    section.className = "metric-section";
    section.dataset.metric = cfg.key;

    var valueSpansHtml = cfg.series.map(function (s) {
      var prefix = s.label ? '<span class="metric-value-tag" style="color:' + s.color + '">' + s.label[0].toUpperCase() + "</span> " : "";
      return prefix + '<span class="value" data-field="' + s.field + '">—</span>';
    }).join('<span class="metric-value-sep"> · </span>');

    section.innerHTML =
      '<div class="metric-section-head">' +
        '<span class="metric-name" style="color:' + cfg.series[0].color + '">' + cfg.name + "</span>" +
        '<span class="metric-value">' + valueSpansHtml + '<span class="unit">' + cfg.unit + "</span></span>" +
      "</div>" +
      '<div class="metric-chart-wrap">' +
        "<canvas></canvas>" +
        '<div class="chart-skeleton">' +
          '<div class="shimmer-bar" style="--i:0"></div><div class="shimmer-bar" style="--i:1"></div>' +
          '<div class="shimmer-bar" style="--i:2"></div><div class="shimmer-bar" style="--i:3"></div>' +
          '<div class="shimmer-bar" style="--i:4"></div><div class="shimmer-bar" style="--i:5"></div>' +
          '<div class="shimmer-bar" style="--i:6"></div><div class="shimmer-bar" style="--i:7"></div>' +
        "</div>" +
        '<div class="chart-error-overlay" hidden><span class="icon" aria-hidden="true">&#9888;</span><span class="msg"></span></div>' +
        '<div class="chart-empty-overlay" hidden>Chưa có dữ liệu</div>' +
        '<div class="tt" hidden><div class="tt-time"></div><div class="tt-rows"></div></div>' +
      "</div>";
    stack.appendChild(section);

    var canvas = section.querySelector("canvas");
    var valueEls = {};
    cfg.series.forEach(function (s) {
      valueEls[s.field] = section.querySelector('.value[data-field="' + s.field + '"]');
    });
    sections[cfg.key] = {
      cfg: cfg,
      canvas: canvas,
      ctx: canvas.getContext("2d"),
      wrap: section.querySelector(".metric-chart-wrap"),
      skeleton: section.querySelector(".chart-skeleton"),
      errorOverlay: section.querySelector(".chart-error-overlay"),
      emptyOverlay: section.querySelector(".chart-empty-overlay"),
      tooltip: section.querySelector(".tt"),
      valueEls: valueEls,
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
    status: "loading", // 'loading' | 'success' | 'error'
    timestamps: [],
    buffers: {}, // field -> [values]
    lastErrorAt: null,
    lastErrorMessage: "",
    hoverIndex: null,
    hoverSection: null,

    init: function () {
      var self = this;
      METRICS.forEach(function (cfg) {
        cfg.series.forEach(function (s) { self.buffers[s.field] = []; });
      });
      this.setLoadingUI(true);
      this.poll();
      // Skip polling while the tab is hidden (no wasted SSH-backed requests),
      // and refresh once the moment the operator returns to the tab.
      setInterval(function () { if (document.hidden) return; self.poll(); }, POLL_INTERVAL_MS);
      document.addEventListener("visibilitychange", function () { if (!document.hidden) self.poll(); });

      var retryBtn = document.getElementById("retry-btn");
      if (retryBtn) retryBtn.addEventListener("click", function () { self.poll(); });
    },

    setLoadingUI: function (loading) {
      var pill = document.querySelector('.node-sidebar-item[data-host="' + cssEscape(host) + '"]');
      if (pill) pill.classList.toggle("is-loading", loading);
      var spinner = document.getElementById("header-spinner");
      var loadingText = document.getElementById("header-loading-text");
      if (spinner) spinner.hidden = !loading;
      if (loadingText) loadingText.hidden = !loading;

      var statusEl = document.getElementById("metrics-status");
      var statusText = document.getElementById("metrics-status-text");
      if (statusEl && statusText) {
        statusEl.classList.toggle("is-loading", loading);
        statusEl.classList.remove("is-stale");
        if (loading) statusText.textContent = "Đang tải dữ liệu " + host + "...";
      }

      METRICS.forEach(function (cfg) {
        var section = sections[cfg.key];
        section.skeleton.hidden = !loading;
        if (loading) {
          section.errorOverlay.hidden = true;
          section.emptyOverlay.hidden = true;
          section.canvas.style.visibility = "hidden";
        }
      });
    },

    poll: function () {
      var self = this;
      var firstLoad = this.timestamps.length === 0 && this.status !== "error";
      if (firstLoad) this.setLoadingUI(true);

      fetch("/api/nodes/" + encodeURIComponent(host) + "/metrics", { credentials: "same-origin" })
        .then(function (response) {
          // require_login raises a 303 to /login on an expired session; fetch's
          // default redirect mode resolves that transparently, landing here as
          // a 200 whose *final* URL is /login — `redirected` tells them apart.
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
        .then(function (data) { self.onSuccess(data); })
        .catch(function (err) {
          if (err.message === "unauthenticated") return;
          self.onError(err.message);
        });
    },

    onSuccess: function (data) {
      this.status = "success";
      this.lastErrorMessage = "";
      this.timestamps.push(new Date());
      if (this.timestamps.length > MAX_POINTS) this.timestamps.shift();

      var self = this;
      METRICS.forEach(function (cfg) {
        cfg.series.forEach(function (s) {
          var v = data[s.field];
          var buf = self.buffers[s.field];
          buf.push(typeof v === "number" ? v : null);
          if (buf.length > MAX_POINTS) buf.shift();
        });
      });

      this.setLoadingUI(false);
      this.setErrorUI(false);
      this.updateValueBadges(data);
      drawAllCharts(true);
    },

    onError: function (message) {
      this.status = "error";
      this.lastErrorAt = new Date();
      this.lastErrorMessage = message;
      this.setLoadingUI(false);
      this.setErrorUI(true, message);
    },

    setErrorUI: function (isError, message) {
      var panel = document.getElementById("metrics-panel");
      var footer = document.getElementById("metrics-error-footer");
      var tsEl = document.getElementById("metrics-error-timestamp");
      var statusEl = document.getElementById("metrics-status");
      var statusText = document.getElementById("metrics-status-text");

      if (panel) panel.classList.toggle("is-error", isError);
      if (footer) footer.hidden = !isError;
      if (statusEl) {
        statusEl.classList.toggle("is-stale", isError);
        statusEl.classList.remove("is-loading");
      }

      METRICS.forEach(function (cfg) {
        var section = sections[cfg.key];
        section.skeleton.hidden = true;
        section.errorOverlay.hidden = !isError;
        section.canvas.style.visibility = isError ? "hidden" : "visible";
        if (isError) {
          section.errorOverlay.querySelector(".msg").textContent = "Không thể kết nối node " + host;
        }
      });

      if (isError) {
        if (tsEl) tsEl.textContent = "Lần thử cuối thất bại lúc " + formatClock(this.lastErrorAt) + " — " + message;
        if (statusText) statusText.textContent = "Mất kết nối node " + host;
      } else if (statusText && this.timestamps.length) {
        statusText.textContent = "Đã cập nhật lúc " + formatClock(this.timestamps[this.timestamps.length - 1]);
      }
    },

    updateValueBadges: function (data) {
      METRICS.forEach(function (cfg) {
        var section = sections[cfg.key];
        cfg.series.forEach(function (s) {
          var el = section.valueEls[s.field];
          if (el) el.textContent = formatValue(cfg, data[s.field]);
        });
      });
    }
  };

  /* ---------- drawing ---------- */
  function drawAllCharts(firstPaint) {
    METRICS.forEach(function (cfg) { drawSection(sections[cfg.key], firstPaint); });
  }

  function drawSection(section, firstPaint) {
    if (App.status !== "success") return; // error/loading overlays own this area instead
    var cfg = section.cfg;
    var timestamps = App.timestamps;
    var n = timestamps.length;

    var allFieldsMissing = cfg.series.every(function (s) {
      var buf = App.buffers[s.field];
      return buf.length === 0 || buf[buf.length - 1] == null;
    });
    section.emptyOverlay.hidden = !allFieldsMissing;
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

    var padLeft = 34, padRight = 8, padTop = 8, padBottom = 18;
    var plotW = Math.max(w - padLeft - padRight, 10);
    var plotH = Math.max(h - padTop - padBottom, 10);

    var yMax;
    if (cfg.fixedMax !== undefined) {
      yMax = cfg.fixedMax;
    } else {
      var dataMax = 0;
      cfg.series.forEach(function (s) {
        App.buffers[s.field].forEach(function (v) { if (v != null && v > dataMax) dataMax = v; });
      });
      yMax = niceMax((dataMax * 1.2) || 1);
    }

    function xAt(i) { return padLeft + (n <= 1 ? 0 : (i / (n - 1)) * plotW); }
    function yAt(v) { return padTop + plotH - (clamp(v, 0, yMax) / (yMax || 1)) * plotH; }
    section.geom = { padLeft: padLeft, plotW: plotW, plotH: plotH, padTop: padTop, n: n, xAt: xAt, yAt: yAt };

    // gridlines — 25/50/75/100 for fixed-percent charts, 0/max for auto-scale ones
    ctx.strokeStyle = GRID_COLOR;
    ctx.lineWidth = 1;
    ctx.font = "10px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
    ctx.fillStyle = "#64748b";
    ctx.textBaseline = "middle";
    var gridValues = cfg.fixedMax !== undefined ? [0, 25, 50, 75, 100] : [0, yMax];
    gridValues.forEach(function (v) {
      var y = Math.round(yAt(v)) + 0.5;
      ctx.beginPath();
      ctx.moveTo(padLeft, y);
      ctx.lineTo(padLeft + plotW, y);
      ctx.stroke();
      ctx.textAlign = "right";
      ctx.fillText(cfg.unit === "%" ? v + "" : (v >= 10 ? v.toFixed(0) : v.toFixed(1)), padLeft - 5, y);
    });

    // x-axis: HH:MM:SS at start / middle / end — deduped, since early in the
    // rolling window (n=1 or 2) "start"/"middle"/"end" can be the same index,
    // which would otherwise draw two differently-aligned labels on top of
    // each other at that one position.
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

    if (allFieldsMissing) return; // flat baseline only, no lines to draw

    cfg.series.forEach(function (s) {
      var values = App.buffers[s.field];
      var offset = n - values.length; // buffers can be shorter than timestamps right after a resize
      drawLine(ctx, values, function (i) { return xAt(i + offset); }, yAt, s.color);
    });

    // direct end-labels instead of a legend, only needed when >1 series
    if (cfg.series.length > 1) {
      ctx.textBaseline = "middle";
      cfg.series.forEach(function (s, si) {
        var values = App.buffers[s.field];
        if (!values.length) return;
        var lastV = values[values.length - 1];
        if (lastV == null) return;
        var x = xAt(n - 1);
        ctx.fillStyle = s.color;
        ctx.textAlign = "right";
        ctx.fillText(s.label, x - 4, yAt(lastV) + (si === 0 ? -8 : 8));
      });
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
      cfg.series.forEach(function (s) {
        var values = App.buffers[s.field];
        var vOffset = n - values.length;
        var idx = App.hoverIndex - vOffset;
        if (idx < 0 || idx >= values.length || values[idx] == null) return;
        var y = yAt(values[idx]);
        ctx.beginPath();
        ctx.arc(xH, y, 3, 0, Math.PI * 2);
        ctx.fillStyle = s.color;
        ctx.fill();
      });
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
    tt.querySelector(".tt-time").textContent = formatClock(ts);
    var rowsEl = tt.querySelector(".tt-rows");
    while (rowsEl.firstChild) rowsEl.removeChild(rowsEl.firstChild);
    section.cfg.series.forEach(function (s) {
      var values = App.buffers[s.field];
      var vOffset = App.timestamps.length - values.length;
      var vIdx = idx - vOffset;
      var v = vIdx >= 0 && vIdx < values.length ? values[vIdx] : null;
      var row = document.createElement("div");
      row.className = "tt-row";
      var key = document.createElement("span"); key.className = "tt-key"; key.style.background = s.color;
      var name = document.createElement("span"); name.className = "tt-name"; name.textContent = s.label || section.cfg.name;
      var val = document.createElement("span"); val.className = "tt-value";
      val.textContent = v == null ? "—" : formatValue(section.cfg, v) + " " + section.cfg.unit;
      row.appendChild(key); row.appendChild(name); row.appendChild(val);
      rowsEl.appendChild(row);
    });
    var wrapWidth = section.wrap.clientWidth;
    var x = g.xAt(idx);
    var translate = x < 84 ? "0" : x > wrapWidth - 84 ? "-100%" : "-50%";
    tt.style.left = x + "px";
    tt.style.transform = "translateX(" + translate + ")";
  }

  function cssEscape(s) {
    return window.CSS && CSS.escape ? CSS.escape(s) : s.replace(/["\\]/g, "\\$&");
  }

  App.init();
  window.addEventListener("resize", function () { drawAllCharts(); });
})();
