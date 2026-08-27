(function () {
  var form = document.getElementById("bucket-access-log-form");
  if (!form) {
    return; // no RGW node configured, or not on this page
  }

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

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    loadLog();
  });

  var historyForm = document.getElementById("bucket-access-history-form");
  if (historyForm) {
    var historyPage = 1, historyPages = 1;
    function loadHistory() {
      var params = new URLSearchParams({cluster: historyForm.dataset.cluster, page: String(historyPage), page_size: "10"});
      [["ip","bah-ip"],["requester","bah-user"],["bucket","bah-bucket"],["method","bah-method"],["date_from","bah-from"],["date_to","bah-to"]].forEach(function (pair) {
        var value = document.getElementById(pair[1]).value.trim();
        if (value) params.set(pair[0], value);
      });
      document.getElementById("bah-status").textContent = "Đang tải…";
      fetch("/api/bucket-access-history?" + params.toString()).then(handleAuthRedirect).then(function (response) {
        if (!response.ok) return response.json().then(function (body) { throw new Error(body.detail || "Không lấy được lịch sử"); });
        return response.json();
      }).then(function (data) {
        var body = document.getElementById("bah-body"); while (body.firstChild) body.removeChild(body.firstChild);
        data.items.forEach(function (r) {
          var tr = document.createElement("tr");
          [formatVnDateTime(r.timestamp), r.request_id || "—", r.ip || "—", r.requester || "—", r.method + " / " + r.action,
           r.bucket || "—", r.object || "—", String(r.status), r.encryption || "—", r.rgw_host].forEach(function (v) { tr.appendChild(cell(v)); });
          body.appendChild(tr);
        });
        historyPages = data.pages; document.getElementById("bah-status").textContent = data.total + " sự kiện — trang " + data.page + "/" + data.pages;
        document.getElementById("bah-prev").disabled = historyPage <= 1;
        document.getElementById("bah-next").disabled = historyPage >= historyPages;
      }).catch(function (err) { document.getElementById("bah-status").textContent = "Lỗi: " + err.message; });
    }
    historyForm.addEventListener("submit", function (event) { event.preventDefault(); historyPage = 1; loadHistory(); });
    document.getElementById("bah-prev").addEventListener("click", function () { if (historyPage > 1) { historyPage -= 1; loadHistory(); } });
    document.getElementById("bah-next").addEventListener("click", function () { if (historyPage < historyPages) { historyPage += 1; loadHistory(); } });
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
