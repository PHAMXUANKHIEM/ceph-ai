(function () {
  var stack = document.getElementById("metrics-stack");
  var nodeSelector = document.getElementById("node-selector");
  var nodeList = document.getElementById("node-list");
  var nodeSnapshotMeta = document.getElementById("node-snapshot-meta");
  var clusterId = nodeSelector ? nodeSelector.dataset.clusterId : "";
  var inventoryRequestInFlight = false;
  var rangeSelect = document.getElementById("node-time-range");
  var RANGE_CONFIG = {
    "2m": { seconds: 120, pollMs: 10000, label: "2 phút" },
    "5m": { seconds: 300, pollMs: 15000, label: "5 phút" },
    "15m": { seconds: 900, pollMs: 30000, label: "15 phút" },
    "1h": { seconds: 3600, pollMs: 30000, label: "1 giờ" },
    "24h": { seconds: 86400, pollMs: 30000, label: "24 giờ" }
  };
  var RANGE_STORAGE_KEY = "ceph-ai.node-monitor.range";

  function normalizeRange(value) {
    value = String(value || "5m").toLowerCase();
    if (RANGE_CONFIG[value]) return value;
    var aliases = { "120": "2m", "300": "5m", "900": "15m", "3600": "1h", "86400": "24h" };
    return aliases[value] || "5m";
  }

  function readStoredRange() {
    try { return localStorage.getItem(RANGE_STORAGE_KEY); } catch (_) { return null; }
  }

  function persistRange(value) {
    try { localStorage.setItem(RANGE_STORAGE_KEY, value); } catch (_) { /* storage may be disabled */ }
    var url = new URL(window.location.href);
    url.searchParams.set("range", value);
    window.history.replaceState({}, "", url.toString());
  }

  var queryRange = new URLSearchParams(window.location.search).get("range");
  var currentRange = normalizeRange(queryRange || readStoredRange() || (rangeSelect && rangeSelect.value));
  if (rangeSelect) rangeSelect.value = currentRange;

  function refreshNodeInventory() {
    if (!nodeSelector || !clusterId || document.hidden || inventoryRequestInFlight) return;
    inventoryRequestInFlight = true;
    fetch("/api/nodes/summary?cluster_id=" + encodeURIComponent(clusterId), { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (payload) {
        var meta = payload.meta || {};
        var data = payload.data || {};
        if (meta.available !== false && nodeList && Array.isArray(data.nodes)) {
          nodeList.replaceChildren();
          var selectedHost = stack ? stack.dataset.host : "";
          data.nodes.forEach(function (node) {
            if (!node || !node.host) return;
            var card = document.createElement("div");
            card.className = "node-selector-card" + (node.host === selectedHost ? " is-selected" : "");
            card.dataset.host = node.host;
            var link = document.createElement("a");
            link.className = "node-selector-link" + (node.host === selectedHost ? " active" : "");
            link.href = "/nodes?cluster=" + encodeURIComponent(clusterId) + "&host=" + encodeURIComponent(node.host);
            var indicator = document.createElement("span");
            indicator.className = "node-health-indicator";
            indicator.setAttribute("aria-hidden", "true");
            var ip = document.createElement("span");
            ip.className = "node-selector-copy";
            var ipText = document.createElement("strong");
            ipText.className = "node-chip-ip";
            ipText.textContent = node.host;
            var roles = document.createElement("small");
            roles.className = "node-role-badges";
            (Array.isArray(node.roles) ? node.roles : []).forEach(function (role) {
              var badge = document.createElement("span");
              badge.className = "role-badge role-badge-" + String(role).toLowerCase();
              badge.textContent = role;
              roles.appendChild(badge);
            });
            ip.appendChild(ipText);
            ip.appendChild(roles);
            link.appendChild(indicator);
            link.appendChild(ip);
            card.appendChild(link);
            nodeList.appendChild(card);
          });
        }
        if (nodeSnapshotMeta) {
          if (meta.last_error) nodeSnapshotMeta.textContent = "Snapshot error: " + meta.last_error;
          else if (meta.collected_at) nodeSnapshotMeta.textContent = "Snapshot generation " + (meta.generation || 0) + " · " + Math.round(meta.age_seconds || 0) + "s" + (meta.stale ? " · stale" : "");
          else nodeSnapshotMeta.textContent = "No snapshot available";
        }
      })
      .catch(function () {
        if (nodeSnapshotMeta) nodeSnapshotMeta.textContent = "Snapshot unavailable";
      })
      .finally(function () { inventoryRequestInFlight = false; });
  }

  if (nodeSelector && clusterId) {
    refreshNodeInventory();
    window.setInterval(refreshNodeInventory, 10000);
    document.addEventListener("visibilitychange", refreshNodeInventory);
  }
  if (!stack) {
    return; // /nodes with no host selected, or not on this page at all
  }

  var host = stack.dataset.host;
  var POLL_INTERVAL_MS = RANGE_CONFIG[currentRange].pollMs;
  var WINDOW_SECONDS = RANGE_CONFIG[currentRange].seconds;
  var pollTimer = null;
  var requestSerial = 0;

  var TOOLTIP_BG = "#0f172a";
  var GRID_COLOR = "#1e293b";
  var CROSSHAIR_COLOR = "#334155";

  var METRICS = [
    {
      key: "cpu_percent", name: "CPU", unit: "%", fixedMax: 100,
      series: [{ field: "cpu_percent", color: "#22c55e" }]
    },
    {
      key: "mem_percent", name: "RAM", unit: "%", fixedMax: 100,
      series: [{ field: "mem_percent", color: "#3b82f6" }]
    },
    {
      key: "disk_iops", name: "Disk IOPS", unit: "ops/s",
      series: [
        { field: "disk_read_iops", label: "read", color: "#22d3ee" },
        { field: "disk_write_iops", label: "write", color: "#f97316" }
      ]
    },
    {
      key: "disk_latency_ms", name: "Disk Latency", unit: "ms",
      series: [{ field: "disk_latency_ms", color: "#eab308" }]
    }
  ];

  function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }

  function pad2(n) { return String(n).padStart(2, "0"); }
  function formatClock(date) {
    return pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds());
  }

  function formatAxisTime(date) {
    if (WINDOW_SECONDS < 3600) return formatClock(date);
    var clock = pad2(date.getHours()) + ":" + pad2(date.getMinutes());
    if (WINDOW_SECONDS >= 86400) {
      var start = App.rangeStart;
      var end = App.rangeEnd;
      if (start && end && start.toDateString() !== end.toDateString()) {
        return pad2(date.getDate()) + "/" + pad2(date.getMonth() + 1) + " " + clock;
      }
    }
    return clock;
  }

  function niceMax(value) {
    if (value <= 0) return 1;
    var exp = Math.floor(Math.log10(value));
    var base = Math.pow(10, exp);
    var frac = value / base;
    var niceFrac = frac <= 1 ? 1 : frac <= 2 ? 2 : frac <= 2.5 ? 2.5 : frac <= 5 ? 5 : 10;
    return niceFrac * base;
  }

  function chartMaxPoints() {
    return { "2m": 24, "5m": 30, "15m": 30, "1h": 40, "24h": 144 }[currentRange];
  }

  function downsampleChartPoints(points, maxPoints) {
    if (points.length <= maxPoints) return points;
    var latest = points[points.length - 1];
    var history = points.slice(0, -1);
    maxPoints = Math.max(maxPoints - 1, 1);
    var bucketSize = Math.ceil(history.length / maxPoints);
    var fields = ["cpu_percent", "mem_percent", "disk_read_iops", "disk_write_iops", "disk_latency_ms"];
    var result = [];
    for (var start = 0; start < history.length; start += bucketSize) {
      var bucket = history.slice(start, start + bucketSize);
      var item = { at: bucket[Math.floor(bucket.length / 2)].at };
      fields.forEach(function (field) {
        var values = bucket.map(function (point) { return point[field]; }).filter(function (value) { return typeof value === "number"; });
        item[field] = values.length ? values.reduce(function (sum, value) { return sum + value; }, 0) / values.length : null;
      });
      result.push(item);
    }
    result.push(latest);
    return result;
  }

  function formatValue(cfg, v) {
    if (v == null) return "—";
    if (cfg.unit === "%") return v.toFixed(1);
    if (cfg.unit === "ms") return v.toFixed(2);
    return v.toFixed(2);
  }

  function metricNumber(value) {
    if (typeof value === "number") return Number.isFinite(value) ? value : null;
    if (typeof value === "string" && value.trim() !== "") {
      var parsed = Number(value);
      return Number.isFinite(parsed) ? parsed : null;
    }
    return null;
  }

  function metricDate(value) {
    if (value instanceof Date) return value;
    if (typeof value === "number") return new Date(value < 100000000000 ? value * 1000 : value);
    var text = String(value || "").trim();
    // Accept both the API's UTC `...Z` value and older `...+00:00Z` values.
    text = text.replace(/\+00:00Z$/, "Z");
    return new Date(text);
  }

  function normalizeMetricPoint(point) {
    if (!point || !point.at) return null;
    var date = metricDate(point.at);
    if (Number.isNaN(date.getTime())) return null;
    var normalized = { at: date.toISOString() };
    ["cpu_percent", "mem_percent", "disk_read_iops", "disk_write_iops", "disk_latency_ms"].forEach(function (field) {
      normalized[field] = metricNumber(point[field]);
    });
    return normalized;
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
        (cfg.series.length > 1 ? '<div class="metric-legend" aria-label="Chú giải biểu đồ">' + cfg.series.map(function (s) { return '<span><i style="background:' + s.color + '"></i>' + s.label + '</span>'; }).join("") + '</div>' : "") +
        '<div class="metric-chart-wrap">' +
        "<canvas></canvas>" +
        '<div class="chart-skeleton">' +
          '<div class="shimmer-bar" style="--i:0"></div><div class="shimmer-bar" style="--i:1"></div>' +
          '<div class="shimmer-bar" style="--i:2"></div><div class="shimmer-bar" style="--i:3"></div>' +
          '<div class="shimmer-bar" style="--i:4"></div><div class="shimmer-bar" style="--i:5"></div>' +
          '<div class="shimmer-bar" style="--i:6"></div><div class="shimmer-bar" style="--i:7"></div>' +
        "</div>" +
        '<div class="chart-loading-overlay" hidden><span class="header-spinner" aria-hidden="true"></span><span>Đang tải dữ liệu ' + RANGE_CONFIG[currentRange].label + '...</span></div>' +
        '<div class="chart-error-overlay" hidden><span class="icon" aria-hidden="true">&#9888;</span><span class="msg"></span></div>' +
        '<div class="chart-empty-overlay" hidden>Chưa có dữ liệu telemetry. Dữ liệu sẽ hiển thị sau vài phút khi hệ thống bắt đầu thu thập.</div>' +
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
      loadingOverlay: section.querySelector(".chart-loading-overlay"),
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
      var targetTime = g.domainStart + clamp(rel, 0, 1) * (g.domainEnd - g.domainStart);
      var idx = 0;
      var closestDistance = Infinity;
      for (var i = 0; i < g.timestamps.length; i++) {
        var distance = Math.abs(g.timestamps[i].getTime() - targetTime);
        if (distance < closestDistance) { closestDistance = distance; idx = i; }
      }
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
    rangeStart: null,
    rangeEnd: null,
    activeRequest: 0,
    livePoints: [],
    rangeKey: currentRange,

    init: function () {
      var self = this;
      METRICS.forEach(function (cfg) {
        cfg.series.forEach(function (s) { self.buffers[s.field] = []; });
      });
      this.setLoadingUI(true);
      this.poll();

      var retryBtn = document.getElementById("retry-btn");
      if (retryBtn) retryBtn.addEventListener("click", function () { self.poll(); });
    },

    schedulePoll: function () {
      var self = this;
      if (pollTimer) window.clearTimeout(pollTimer);
      pollTimer = window.setTimeout(function () { self.poll(); }, POLL_INTERVAL_MS);
    },

    setLoadingUI: function (loading, rangeRefresh) {
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
        if (section.loadingOverlay) {
          section.loadingOverlay.querySelector("span:last-child").textContent = "Đang tải dữ liệu " + RANGE_CONFIG[currentRange].label + "...";
          section.loadingOverlay.hidden = !loading;
        }
        section.skeleton.hidden = !loading || !!rangeRefresh;
        if (loading) {
          section.errorOverlay.hidden = true;
          section.emptyOverlay.hidden = true;
          if (!rangeRefresh) section.canvas.style.visibility = "hidden";
        }
      });
    },

    poll: function () {
      var self = this;
      var firstLoad = this.timestamps.length === 0 && this.status !== "error";
      if (firstLoad) this.setLoadingUI(true);
      var requestId = ++requestSerial;
      this.activeRequest = requestId;

      fetch("/api/nodes/" + encodeURIComponent(host) + "/metrics?cluster_id=" + encodeURIComponent(clusterId) + "&range=" + encodeURIComponent(currentRange), { credentials: "same-origin" })
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
        .then(function (data) { if (requestId === self.activeRequest) self.onSuccess(data); })
        .catch(function (err) {
          if (err.message === "unauthenticated") return;
          if (requestId === self.activeRequest) self.onError(err.message);
        })
        .finally(function () {
          if (requestId === self.activeRequest) self.schedulePoll();
        });
    },

    onSuccess: function (data) {
      this.status = "success";
      this.lastErrorMessage = "";
      var self = this;
      if (this.rangeKey !== currentRange) {
        this.livePoints = [];
        this.rangeKey = currentRange;
      }
      var rawPoints = Array.isArray(data.points) ? data.points : (Array.isArray(data.data) ? data.data : []);
      var responsePoints = rawPoints.map(normalizeMetricPoint).filter(Boolean);
      var currentPoint = normalizeMetricPoint(data.current && data.current.at ? data.current : data);
      console.debug("[Node Monitoring] metrics response", {
        host: host,
        range: currentRange,
        apiRange: data.range,
        sampleCount: data.sample_count,
        sourceSampleCount: data.source_sample_count,
        receivedPoints: rawPoints.length,
        validPoints: responsePoints.length,
        firstPoint: responsePoints[0] || null,
        lastPoint: responsePoints[responsePoints.length - 1] || null
      });
      if (currentPoint) this.livePoints.push(currentPoint);
      var cutoffMs = Date.now() - WINDOW_SECONDS * 1000;
      this.livePoints = this.livePoints.filter(function (point) {
        return point && metricDate(point.at).getTime() >= cutoffMs;
      });
      // Merge the server history with live samples collected between polls.
      // The timestamp map also removes the current point duplicated by the
      // response and makes a range change deterministic.
      var pointByTimestamp = {};
      responsePoints.concat(this.livePoints).forEach(function (point) {
        if (point && point.at && !Number.isNaN(metricDate(point.at).getTime())) pointByTimestamp[point.at] = point;
      });
      var points = Object.keys(pointByTimestamp).map(function (key) { return pointByTimestamp[key]; }).sort(function (a, b) {
        return metricDate(a.at).getTime() - metricDate(b.at).getTime();
      });
      points = downsampleChartPoints(points, chartMaxPoints());
      this.timestamps = [];
      METRICS.forEach(function (cfg) {
        cfg.series.forEach(function (s) {
          self.buffers[s.field] = [];
        });
      });
      points.forEach(function (point) {
        var timestamp = metricDate(point.at);
        if (Number.isNaN(timestamp.getTime())) return;
        self.timestamps.push(timestamp);
        METRICS.forEach(function (cfg) {
          cfg.series.forEach(function (s) {
            self.buffers[s.field].push(metricNumber(point[s.field]));
          });
        });
      });
      this.rangeStart = data.range_start ? metricDate(data.range_start) : new Date(Date.now() - WINDOW_SECONDS * 1000);
      this.rangeEnd = data.range_end ? metricDate(data.range_end) : new Date();

      this.setLoadingUI(false);
      this.setErrorUI(false);
      this.updateValueBadges(currentPoint || data);
      this.updateSummary(data.summary || data.current || data, data);
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
    },

    updateSummary: function (data) {
      function setText(id, value, digits) {
        var el = document.getElementById(id);
        if (el) el.textContent = value == null ? "—" : Number(value).toFixed(digits);
      }
      function setMeter(id, value, max) {
        var el = document.getElementById(id);
        if (el) el.style.width = value == null ? "0%" : clamp(Number(value) / max * 100, 0, 100) + "%";
      }
      setText("summary-cpu", data.cpu_percent, 1);
      setMeter("summary-cpu-meter", data.cpu_percent, 100);
      setText("summary-ram", data.mem_percent, 1);
      setMeter("summary-ram-meter", data.mem_percent, 100);
      var read = data.disk_read_iops;
      var write = data.disk_write_iops;
      setText("summary-read-iops", read, 0);
      setText("summary-write-iops", write, 0);
      setText("summary-iops", read == null || write == null ? null : Number(read) + Number(write), 0);
      setText("summary-latency", data.disk_latency_ms, 2);
      setMeter("summary-latency-meter", data.disk_latency_ms, 20);
      var context = "(avg " + RANGE_CONFIG[currentRange].label + ")";
      ["cpu", "ram", "iops", "latency"].forEach(function (metric) {
        var contextEl = document.getElementById("summary-" + metric + "-context");
        if (contextEl) contextEl.textContent = context;
      });
      var selectedCard = document.querySelector('.node-selector-card[data-host="' + cssEscape(host) + '"]');
      if (selectedCard) selectedCard.classList.add("is-healthy");
    }
  };

  if (rangeSelect) {
    rangeSelect.addEventListener("change", function () {
      currentRange = normalizeRange(rangeSelect.value);
      WINDOW_SECONDS = RANGE_CONFIG[currentRange].seconds;
      POLL_INTERVAL_MS = RANGE_CONFIG[currentRange].pollMs;
      persistRange(currentRange);
      App.status = "loading";
      App.rangeStart = new Date(Date.now() - WINDOW_SECONDS * 1000);
      App.rangeEnd = new Date();
      App.setLoadingUI(true, true);
      App.poll();
    });
  }

  /* ---------- drawing ---------- */
  function drawAllCharts(firstPaint) {
    METRICS.forEach(function (cfg) { drawSection(sections[cfg.key], firstPaint); });
  }

  function drawSection(section, firstPaint) {
    if (App.status !== "success") return; // error/loading overlays own this area instead
    var cfg = section.cfg;
    var timestamps = App.timestamps;
    var n = timestamps.length;

    var hasData = cfg.series.some(function (s) {
      var buf = App.buffers[s.field];
      return buf.some(function (value) { return value != null; });
    });
    section.emptyOverlay.hidden = hasData;
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

    var domainStart = App.rangeStart && !Number.isNaN(App.rangeStart.getTime())
      ? App.rangeStart.getTime() : (timestamps[0] ? timestamps[0].getTime() : Date.now() - WINDOW_SECONDS * 1000);
    var domainEnd = App.rangeEnd && !Number.isNaN(App.rangeEnd.getTime())
      ? App.rangeEnd.getTime() : domainStart + WINDOW_SECONDS * 1000;
    if (domainEnd <= domainStart) domainEnd = domainStart + WINDOW_SECONDS * 1000;
    function xAt(i) {
      if (!timestamps[i]) return padLeft;
      return padLeft + clamp((timestamps[i].getTime() - domainStart) / (domainEnd - domainStart), 0, 1) * plotW;
    }
    function yAt(v) { return padTop + plotH - (clamp(v, 0, yMax) / (yMax || 1)) * plotH; }
    section.geom = {
      padLeft: padLeft, plotW: plotW, plotH: plotH, padTop: padTop, n: n,
      xAt: xAt, yAt: yAt, domainStart: domainStart, domainEnd: domainEnd,
      timestamps: timestamps
    };

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

    // x-axis uses the selected time window, rather than the first/last sample.
    // This keeps a sparse 24h history visibly distinct from a 2m history.
    ctx.textBaseline = "top";
    var tickTimes = [domainStart, domainStart + (domainEnd - domainStart) / 2, domainEnd];
    tickTimes.forEach(function (time, i) {
      var tickX = padLeft + (time - domainStart) / (domainEnd - domainStart) * plotW;
      ctx.textAlign = i === 0 ? "left" : i === tickTimes.length - 1 ? "right" : "center";
      ctx.fillText(formatAxisTime(new Date(time)), tickX, padTop + plotH + 4);
    });

    if (!hasData) return;

    cfg.series.forEach(function (s) {
      var values = App.buffers[s.field];
      var offset = n - values.length; // buffers can be shorter than timestamps right after a resize
      drawLine(ctx, values, function (i) { return xAt(i + offset); }, yAt, s.color, timestamps, yAt(0), offset);
    });

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

  function hexRgba(color, alpha) {
    var match = String(color).match(/^#([0-9a-f]{6})$/i);
    if (!match) return color;
    var hex = match[1];
    return "rgba(" + parseInt(hex.slice(0, 2), 16) + "," + parseInt(hex.slice(2, 4), 16) + "," + parseInt(hex.slice(4, 6), 16) + "," + alpha + ")";
  }

  function gapThreshold(timestamps) {
    var gaps = [];
    for (var i = 1; i < timestamps.length; i++) {
      var gap = timestamps[i].getTime() - timestamps[i - 1].getTime();
      if (gap > 0) gaps.push(gap);
    }
    if (!gaps.length) return Infinity;
    gaps.sort(function (a, b) { return a - b; });
    var median = gaps[Math.floor(gaps.length / 2)];
    return Math.max(median * 2.5, 2 * 60 * 1000);
  }

  function drawLine(ctx, values, xAt, yAt, color, timestamps, baseline, offset) {
    var maxGap = gapThreshold(timestamps);
    var segments = [], segment = [];
    var lastTime = null;
    for (var i = 0; i < values.length; i++) {
      var value = values[i];
      var timestamp = timestamps[i + offset];
      var time = timestamp && timestamp.getTime();
      if (value == null || !timestamp || (lastTime != null && time - lastTime > maxGap)) {
        if (segment.length) segments.push(segment);
        segment = [];
        lastTime = null;
        if (value == null) continue;
      }
      segment.push({ x: xAt(i), y: yAt(value) });
      lastTime = time;
    }
    if (segment.length) segments.push(segment);

    var gradient = ctx.createLinearGradient(0, 0, 0, baseline);
    gradient.addColorStop(0, hexRgba(color, 0.28));
    gradient.addColorStop(1, hexRgba(color, 0));
    segments.forEach(function (points) {
      ctx.beginPath();
      ctx.moveTo(points[0].x, baseline);
      points.forEach(function (point) { ctx.lineTo(point.x, point.y); });
      ctx.lineTo(points[points.length - 1].x, baseline);
      ctx.closePath();
      ctx.fillStyle = gradient;
      ctx.fill();

      ctx.beginPath();
      points.forEach(function (point, index) {
        if (index === 0) ctx.moveTo(point.x, point.y);
        else ctx.lineTo(point.x, point.y);
      });
      ctx.strokeStyle = color;
      ctx.lineWidth = 2;
      ctx.lineJoin = "round";
      ctx.lineCap = "round";
      ctx.stroke();
      if (points.length === 1) {
        ctx.beginPath();
        ctx.arc(points[0].x, points[0].y, 3.5, 0, Math.PI * 2);
        ctx.fillStyle = color;
        ctx.fill();
      }
    });
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
