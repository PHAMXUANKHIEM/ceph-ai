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
      tr.appendChild(cell(r.action));
      tr.appendChild(cell(r.method));
      tr.appendChild(cell(r.bucket || "—"));
      tr.appendChild(cell(r.object || "—"));
      tr.appendChild(statusCell(r.status));
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
})();
