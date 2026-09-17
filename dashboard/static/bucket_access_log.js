(function () {
  var form = document.getElementById("bucket-access-log-form");
  var hostSelect = document.getElementById("bal-host");
  var bucketInput = document.getElementById("bal-bucket");
  var statusEl = document.getElementById("bal-status");
  var tableWrap = document.getElementById("bal-table-wrap");
  var tableBody = document.getElementById("bal-table-body");
  var bucketInfoWrap = document.getElementById("bal-bucket-info-wrap");
  var bucketInfoBody = document.getElementById("bal-bucket-info-body");

  function formatBytes(bytes) {
    if (bytes === null || bytes === undefined) return "—";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var value = bytes;
    var i = 0;
    while (value >= 1024 && i < units.length - 1) {
      value = value / 1024;
      i += 1;
    }
    return value.toFixed(i === 0 ? 0 : 1) + " " + units[i];
  }

  // Always renders in Asia/Ho_Chi_Minh regardless of the viewing browser's
  // own OS timezone — same helper/reasoning as
  // dashboard/static/nodes_rgw_log.js's formatClock.
  function formatVnDateTime(iso) {
    if (!iso) return "—";
    var d = new Date(iso);
    if (isNaN(d.getTime())) return "—";
    var parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Ho_Chi_Minh",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false,
    }).formatToParts(d);
    var map = {};
    parts.forEach(function (p) { map[p.type] = p.value; });
    return map.day + "/" + map.month + "/" + map.year + " " + map.hour + ":" + map.minute + ":" + map.second;
  }

  function handleAuthRedirect(response) {
    if (response.redirected && response.url.indexOf("/login") !== -1) {
      window.location.reload();
      throw new Error("unauthenticated");
    }
    return response;
  }

  function cell(text) {
    var td = document.createElement("td");
    td.textContent = text;
    return td;
  }

  function statusCell(status) {
    var td = document.createElement("td");
    var isError = status >= 400;
    td.textContent = String(status);
    if (isError) td.className = "progress-item-failed";
    return td;
  }

  function infoRow(label, value) {
    // Both <td> (not <th>) — this codebase's table CSS only styles
    // `tbody td`/`thead th`, never a `<th>` sitting inside `<tbody>`.
    var tr = document.createElement("tr");
    var labelCell = document.createElement("td");
    var strong = document.createElement("strong");
    strong.textContent = label;
    labelCell.appendChild(strong);
    tr.appendChild(labelCell);
    tr.appendChild(cell(value));
    return tr;
  }

  function renderBucketInfo(stats) {
    while (bucketInfoBody.firstChild) bucketInfoBody.removeChild(bucketInfoBody.firstChild);
    if (!stats) {
      bucketInfoWrap.hidden = true;
      return;
    }
    bucketInfoBody.appendChild(infoRow("Chủ sở hữu", stats.owner || "—"));
    bucketInfoBody.appendChild(infoRow("Ngày tạo", formatVnDateTime(stats.creation_time)));
    bucketInfoBody.appendChild(infoRow("Số object", String(stats.num_objects)));
    bucketInfoBody.appendChild(infoRow("Dung lượng", formatBytes(stats.size_bytes)));
    bucketInfoBody.appendChild(
      infoRow(
        "Quota",
        stats.quota_enabled
          ? formatBytes(stats.quota_max_size_bytes) + " / " + (stats.quota_max_objects === -1 ? "không giới hạn object" : stats.quota_max_objects + " object")
          : "Không đặt"
      )
    );
    bucketInfoWrap.hidden = false;
  }

  function renderRecords(records) {
    while (tableBody.firstChild) tableBody.removeChild(tableBody.firstChild);
    if (!records.length) {
      tableWrap.hidden = true;
      statusEl.textContent = "Không có request nào khớp.";
      return;
    }
    records.forEach(function (r) {
      var tr = document.createElement("tr");
      tr.appendChild(cell(formatVnDateTime(r.timestamp)));
      tr.appendChild(cell(r.remote_addr));
      tr.appendChild(cell(r.requester || "—"));
      tr.appendChild(cell(r.user_agent || "—"));
      tr.appendChild(cell(r.action));
      tr.appendChild(cell(r.method));
      tr.appendChild(cell(r.bucket || "—"));
      tr.appendChild(cell(r.object || "—"));
      tr.appendChild(statusCell(r.status));
      tr.appendChild(cell(formatBytes(r.bytes_sent)));
      tr.appendChild(cell(r.latency_ms === null || r.latency_ms === undefined ? "—" : r.latency_ms.toFixed(3) + " ms"));
      tableBody.appendChild(tr);
    });
    tableWrap.hidden = false;
    statusEl.textContent = records.length + " request.";
  }

  function loadLog() {
    var host = hostSelect.value;
    var bucket = bucketInput.value.trim();
    statusEl.textContent = "Đang tải...";
    tableWrap.hidden = true;
    bucketInfoWrap.hidden = true;

    var url = "/api/bucket-access-log?host=" + encodeURIComponent(host);
    if (bucket) url += "&bucket=" + encodeURIComponent(bucket);

    fetch(url)
      .then(handleAuthRedirect)
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (body) {
            throw new Error((body && body.detail) || "Không lấy được log");
          });
        }
        return response.json();
      })
      .then(function (data) {
        renderBucketInfo(data.bucket_stats);
        renderRecords(data.records);
      })
      .catch(function (err) {
        tableWrap.hidden = true;
        bucketInfoWrap.hidden = true;
        statusEl.textContent = "Lỗi: " + err.message;
      });
  }

  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      loadLog();
    });
  }

  var historyForm = document.getElementById("bucket-access-history-form");
  if (historyForm) {
    var historyPage = 1, historyPages = 1, historyPageSize = 10;
    var historyRequest = null;
    var searchInput = document.getElementById("bah-search");
    var methodInput = document.getElementById("bah-method");
    var fromInput = document.getElementById("bah-from");
    var toInput = document.getElementById("bah-to");
    var dateToggle = document.getElementById("bah-date-toggle");
    var datePanel = document.getElementById("bah-date-panel");
    var dateLabel = document.getElementById("bah-date-label");
    var pageSizeInput = document.getElementById("bah-page-size");
    var pageNumbers = document.getElementById("bah-pages");
    var tableWrap = document.querySelector(".bucket-audit-table-frame");
    var emptyEl = document.getElementById("bah-empty");

    function shortDate(value) {
      if (!value) return "";
      var parts = value.split("T");
      var date = parts[0].split("-");
      return date.length === 3 ? date[2] + "/" + date[1] + "/" + date[0] + (parts[1] ? " " + parts[1].slice(0, 5) : "") : value;
    }

    function syncDateLabel() {
      var from = shortDate(fromInput.value);
      var to = shortDate(toInput.value);
      dateLabel.textContent = from || to ? (from || "…") + " — " + (to || "…") : "Từ ngày — Đến ngày";
    }

    function relativeTime(iso) {
      if (!iso) return "—";
      var time = new Date(iso).getTime();
      if (isNaN(time)) return "—";
      var seconds = Math.max(0, Math.round((Date.now() - time) / 1000));
      if (seconds < 60) return "Vừa xong";
      if (seconds < 3600) return Math.floor(seconds / 60) + " phút trước";
      if (seconds < 86400) return Math.floor(seconds / 3600) + " giờ trước";
      return Math.floor(seconds / 86400) + " ngày trước";
    }

    function statusGroup(status) {
      var code = Number(status);
      if (code >= 200 && code < 300) return "2xx";
      if (code >= 300 && code < 400) return "3xx";
      if (code >= 400 && code < 500) return "4xx";
      if (code >= 500) return "5xx";
      return "other";
    }

    function renderSummary(data) {
      var summary = data.summary || {};
      var counts = summary.status_counts || {};
      if (!Object.keys(counts).length) {
        (data.items || []).forEach(function (item) { counts[String(item.status)] = (counts[String(item.status)] || 0) + 1; });
      }
      var groups = {"2xx": 0, "3xx": 0, "4xx": 0, "5xx": 0};
      Object.keys(counts).forEach(function (key) { var group = statusGroup(key); if (groups[group] !== undefined) groups[group] += Number(counts[key]) || 0; });
      var total = Object.keys(groups).reduce(function (sum, key) { return sum + groups[key]; }, 0);
      document.getElementById("bah-summary-total").textContent = String(data.total == null ? total : data.total);
      var bar = document.getElementById("bah-summary-status-bar");
      var legend = document.getElementById("bah-summary-status-legend");
      bar.replaceChildren(); legend.replaceChildren();
      ["2xx", "3xx", "4xx", "5xx"].forEach(function (key) {
        if (!groups[key]) return;
        var segment = document.createElement("span");
        segment.className = "bucket-audit-status-segment is-" + key;
        segment.style.width = (total ? groups[key] / total * 100 : 0) + "%";
        segment.title = key + ": " + groups[key];
        bar.appendChild(segment);
        var legendItem = document.createElement("span");
        legendItem.className = "bucket-audit-legend-item is-" + key;
        legendItem.textContent = key + " " + groups[key];
        legend.appendChild(legendItem);
      });
      if (!bar.children.length) {
        bar.classList.add("is-empty");
        var noStatus = document.createElement("span"); noStatus.textContent = "Chưa có dữ liệu"; bar.appendChild(noStatus);
      } else bar.classList.remove("is-empty");

      var topIp = summary.top_ip;
      if (!topIp && data.items && data.items.length) {
        var byIp = {};
        data.items.forEach(function (item) { if (item.ip) byIp[item.ip] = (byIp[item.ip] || 0) + 1; });
        Object.keys(byIp).sort(function (a, b) { return byIp[b] - byIp[a]; }).slice(0, 1).forEach(function (ip) { topIp = { value: ip, count: byIp[ip] }; });
      }
      document.getElementById("bah-summary-top-ip").textContent = topIp && topIp.value ? topIp.value : "—";
      document.getElementById("bah-summary-top-ip-count").textContent = topIp ? topIp.count + " request theo bộ lọc" : "Chưa có dữ liệu";
      var latest = summary.latest_at || (data.items && data.items[0] && data.items[0].timestamp);
      document.getElementById("bah-summary-latest").textContent = relativeTime(latest);
      document.getElementById("bah-summary-latest-absolute").textContent = latest ? formatVnDateTime(latest) : "Chưa có dữ liệu";
    }

    function httpBadge(status) {
      var span = document.createElement("span");
      var group = statusGroup(status);
      span.className = "bucket-audit-http-badge is-" + group;
      span.textContent = String(status == null ? "—" : status);
      span.title = "HTTP " + span.textContent;
      return span;
    }

    function actionCell(record) {
      var div = document.createElement("div");
      div.className = "bucket-audit-action";
      var method = document.createElement("span");
      method.className = "bucket-audit-method-badge is-" + String(record.method || "unknown").toLowerCase();
      method.textContent = record.method || "?";
      var action = document.createElement("span");
      action.textContent = record.action || "unknown";
      div.appendChild(method); div.appendChild(action);
      return div;
    }

    function targetLabel(record) {
      if (!record.bucket && !record.object) return "—";
      return [record.bucket, record.object].filter(Boolean).join("/");
    }

    function copyText(value, button) {
      var done = function () { button.textContent = "Đã copy"; window.setTimeout(function () { button.textContent = "Copy"; }, 1200); };
      if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(value).then(done).catch(function () {});
      else { var area = document.createElement("textarea"); area.value = value; document.body.appendChild(area); area.select(); document.execCommand("copy"); area.remove(); done(); }
    }

    function detailRow(record) {
      var tr = document.createElement("tr");
      tr.className = "bucket-audit-detail-row";
      tr.hidden = true;
      var td = document.createElement("td"); td.colSpan = 6;
      var panel = document.createElement("div"); panel.className = "bucket-audit-detail-panel";
      var request = document.createElement("div"); request.className = "bucket-audit-detail-request";
      var requestLabel = document.createElement("span"); requestLabel.textContent = "Request ID";
      var requestValue = document.createElement("code"); requestValue.textContent = record.request_id || "—";
      var copy = document.createElement("button"); copy.type = "button"; copy.className = "btn btn-ghost btn-sm"; copy.textContent = "Copy"; copy.disabled = !record.request_id;
      copy.addEventListener("click", function (event) { event.stopPropagation(); if (record.request_id) copyText(record.request_id, copy); });
      request.appendChild(requestLabel); request.appendChild(requestValue); request.appendChild(copy); panel.appendChild(request);
      var grid = document.createElement("dl"); grid.className = "bucket-audit-detail-grid";
      [["Thời gian đầy đủ", formatVnDateTime(record.timestamp)], ["IP thực hiện", record.ip || "—"], ["User-Agent", record.user_agent || "Chưa có dữ liệu"], ["Bucket / Object", targetLabel(record)], ["HTTP status", String(record.status == null ? "—" : record.status)], ["Response size", formatBytes(record.size)], ["Mã hóa", record.encryption || "—"], ["RGW host", record.rgw_host || "—"]].forEach(function (pair) {
        var wrap = document.createElement("div"); var dt = document.createElement("dt"); dt.textContent = pair[0]; var dd = document.createElement("dd"); dd.textContent = pair[1]; wrap.appendChild(dt); wrap.appendChild(dd); grid.appendChild(wrap);
      });
      panel.appendChild(grid); td.appendChild(panel); tr.appendChild(td); return tr;
    }

    function toggleDetail(row, detail) {
      var open = detail.hidden;
      detail.hidden = !open;
      row.setAttribute("aria-expanded", String(open));
      row.classList.toggle("is-expanded", open);
    }

    function renderHistoryRows(items) {
      var body = document.getElementById("bah-body");
      body.replaceChildren();
      (items || []).forEach(function (record) {
        var row = document.createElement("tr");
        row.className = "bucket-audit-data-row";
        row.tabIndex = 0; row.setAttribute("aria-expanded", "false");
        var target = targetLabel(record);
        var time = document.createElement("time"); time.dateTime = record.timestamp || ""; time.textContent = formatVnDateTime(record.timestamp); time.title = record.timestamp || "";
        var timeCell = document.createElement("td"); timeCell.appendChild(time);
        row.appendChild(timeCell);
        row.appendChild(cell(record.ip || "—"));
        var userCell = cell(record.requester || "—"); userCell.className = "bah-col-user"; row.appendChild(userCell);
        var action = document.createElement("td"); action.appendChild(actionCell(record)); row.appendChild(action);
        var targetCell = cell(target); targetCell.className = "bah-col-target"; targetCell.title = target; row.appendChild(targetCell);
        var http = document.createElement("td"); http.appendChild(httpBadge(record.status)); row.appendChild(http);
        var detail = detailRow(record);
        row.addEventListener("click", function (event) { if (event.target.closest("button, a")) return; toggleDetail(row, detail); });
        row.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggleDetail(row, detail); } });
        body.appendChild(row); body.appendChild(detail);
      });
    }

    function renderPageNumbers() {
      if (window.DashboardPagination) {
        window.DashboardPagination.renderPages(pageNumbers, historyPage, historyPages, function (targetPage) { historyPage = targetPage; loadHistory(); });
        return;
      }
      pageNumbers.replaceChildren();
      var start = Math.max(1, historyPage - 2), end = Math.min(historyPages, start + 4);
      start = Math.max(1, end - 4);
      for (var page = start; page <= end; page += 1) {
        var button = document.createElement("button"); button.type = "button"; button.className = "pagination-page-number bucket-audit-page-number" + (page === historyPage ? " is-active" : ""); button.textContent = String(page); button.setAttribute("aria-label", "Trang " + page); button.setAttribute("aria-current", page === historyPage ? "page" : "false");
        button.disabled = page === historyPage; button.addEventListener("click", (function (targetPage) { return function () { historyPage = targetPage; loadHistory(); }; })(page)); pageNumbers.appendChild(button);
      }
    }

    function loadHistory() {
      if (historyRequest) historyRequest.abort();
      historyRequest = window.AbortController ? new AbortController() : null;
      var params = new URLSearchParams({cluster: historyForm.dataset.cluster, page: String(historyPage), page_size: String(historyPageSize)});
      var search = searchInput.value.trim();
      if (search) params.set("search", search);
      var initialBucket = document.getElementById("bah-bucket").value.trim();
      if (initialBucket && !search) params.set("bucket", initialBucket);
      if (methodInput.value) params.set("method", methodInput.value);
      if (fromInput.value) params.set("date_from", fromInput.value);
      if (toInput.value) params.set("date_to", toInput.value);
      document.getElementById("bah-status").textContent = "Đang tải…";
      fetch("/api/bucket-access-history?" + params.toString(), { credentials: "same-origin", signal: historyRequest ? historyRequest.signal : undefined }).then(handleAuthRedirect).then(function (response) {
        if (!response.ok) return response.json().then(function (body) { throw new Error(body.detail || "Không lấy được lịch sử"); });
        return response.json();
      }).then(function (data) {
        renderHistoryRows(data.items || []); renderSummary(data); historyPages = data.pages || 1; historyPage = data.page || historyPage;
        document.getElementById("bah-status").textContent = data.total + " sự kiện · cập nhật gần thời gian thực";
        var first = data.total ? ((historyPage - 1) * historyPageSize + 1) : 0;
        var last = Math.min(historyPage * historyPageSize, data.total);
        var summary = "Hiển thị " + first + "–" + last + " / " + data.total + " mục";
        document.getElementById("bah-total-label").textContent = summary;
        document.getElementById("bah-total-label").dataset.mobileSummary = first + "–" + last + " / " + data.total;
        document.getElementById("bah-page-label").textContent = "Trang " + historyPage + "/" + historyPages;
        document.getElementById("bah-prev").disabled = historyPage <= 1; document.getElementById("bah-next").disabled = historyPage >= historyPages;
        emptyEl.hidden = (data.items || []).length > 0; tableWrap.classList.toggle("is-empty", !(data.items || []).length); renderPageNumbers();
      }).catch(function (err) { if (err.name !== "AbortError") document.getElementById("bah-status").textContent = "Lỗi: " + err.message; });
    }
    historyForm.addEventListener("submit", function (event) { event.preventDefault(); historyPage = 1; loadHistory(); });
    document.getElementById("bah-prev").addEventListener("click", function () { if (historyPage > 1) { historyPage -= 1; loadHistory(); } });
    document.getElementById("bah-next").addEventListener("click", function () { if (historyPage < historyPages) { historyPage += 1; loadHistory(); } });
    pageSizeInput.addEventListener("change", function () { historyPageSize = Number(pageSizeInput.value) || 10; historyPage = 1; loadHistory(); });
    dateToggle.addEventListener("click", function () { var open = datePanel.hidden; datePanel.hidden = !open; dateToggle.setAttribute("aria-expanded", String(open)); });
    document.getElementById("bah-date-apply").addEventListener("click", function () { syncDateLabel(); datePanel.hidden = true; dateToggle.setAttribute("aria-expanded", "false"); });
    fromInput.addEventListener("change", syncDateLabel); toInput.addEventListener("change", syncDateLabel); syncDateLabel();

    var purgeButton = document.getElementById("bah-purge");
    if (purgeButton) {
      purgeButton.addEventListener("click", function () {
        if (!window.confirm("Xoá vĩnh viễn TOÀN BỘ lịch sử IP thao tác Bucket/Object của cụm này? Không thể hoàn tác.")) {
          return;
        }
        purgeButton.disabled = true;
        fetch("/api/bucket-access-history/purge?cluster=" + encodeURIComponent(historyForm.dataset.cluster), {
          method: "POST", credentials: "same-origin",
        }).then(handleAuthRedirect).then(function (response) {
          if (!response.ok) return response.json().then(function (body) { throw new Error(body.detail || "Xoá thất bại"); });
          return response.json();
        }).then(function (data) {
          historyPage = 1;
          loadHistory();
          document.getElementById("bah-status").textContent = "Đã xoá " + data.deleted + " bản ghi.";
        }).catch(function (err) {
          document.getElementById("bah-status").textContent = "Lỗi: " + err.message;
        }).finally(function () { purgeButton.disabled = false; });
      });
    }

    loadHistory();
  }

  var configForm = document.getElementById("bucket-logging-config-form");
  if (configForm) {
    var previewData = null;
    var configStatus = document.getElementById("bl-config-status");
    function configPayload() {
      return {action: document.getElementById("bl-action").value,
        source_bucket: document.getElementById("bl-source").value.trim(),
        target_bucket: document.getElementById("bl-target").value.trim(),
        prefix: document.getElementById("bl-prefix").value,
        owner: document.getElementById("bl-owner").value.trim(),
        endpoint: document.getElementById("bl-endpoint").value.trim()};
    }
    function configUrl(kind) { return "/api/bucket-logging/" + kind + "?cluster=" + encodeURIComponent(configForm.dataset.cluster); }
    configForm.addEventListener("submit", async function (event) {
      event.preventDefault(); configStatus.textContent = "Đang kiểm tra version và target…";
      var response = await fetch(configUrl("preview"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(configPayload())});
      var body = await response.json();
      if (!response.ok) { configStatus.textContent = "Lỗi: " + (body.detail || "Preview thất bại"); return; }
      previewData = body;
      var preview = document.getElementById("bl-preview"); preview.hidden = false;
      preview.textContent = "Mode: " + body.mode + "\nCeph: " + body.ceph_version + "\nSource: " + body.source_bucket + "\nTarget: " + (body.target_bucket || "—") + "\nPrefix: " + body.prefix + (body.warning ? "\nCảnh báo: " + body.warning : "");
      document.getElementById("bl-confirm-wrap").hidden = false; configStatus.textContent = "Preview sẵn sàng.";
    });
    document.getElementById("bl-execute").addEventListener("click", async function () {
      if (!previewData) return;
      var payload = configPayload(); payload.confirmation = document.getElementById("bl-confirm").value;
      var response = await fetch(configUrl("execute"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      var body = await response.json();
      configStatus.textContent = response.ok ? "Đã áp dụng chế độ " + body.mode + "." : "Lỗi: " + (body.detail || "Không áp dụng được");
    });
  }
})();
