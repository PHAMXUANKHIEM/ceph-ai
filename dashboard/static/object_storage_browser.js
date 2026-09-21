(function () {
  "use strict";
  const loading = document.getElementById("bucket-detail-loading");
  if (loading) {
    const message = loading.querySelector("p");
    async function pollDetail() {
      try {
        const params = new URLSearchParams({cluster: loading.dataset.cluster});
        const response = await fetch("/api/object-storage/buckets/" + encodeURIComponent(loading.dataset.bucket) + "?" + params, {cache: "no-store"});
        const body = await response.json();
        if (!response.ok) throw new Error(body.detail || "Không tải được metadata bucket");
        if (body.ready) {
          window.location.reload();
          return;
        }
        setTimeout(pollDetail, 1000);
      } catch (error) {
        if (message) message.textContent = "Đang chờ RGW phản hồi: " + error.message;
        setTimeout(pollDetail, 2000);
      }
    }
    pollDetail();
    return;
  }
  const root = document.getElementById("object-browser");
  if (!root) return;
  const form = document.getElementById("object-browser-form");
  const rows = document.getElementById("object-browser-rows");
  const status = document.getElementById("object-browser-status");
  const next = document.getElementById("object-browser-next");
  let marker = "";
  const versionCard = document.getElementById("object-version-actions");
  let selectedVersionItem = null;
  let approvedVersionAction = null;

  async function loadActivity() {
    const activity = document.getElementById("bucket-activity");
    if (!activity) return;
    const activityStatus = document.getElementById("bucket-activity-status");
    const activityTable = document.getElementById("bucket-activity-table");
    const activityRows = document.getElementById("bucket-activity-rows");
    try {
      const params = new URLSearchParams({cluster: activity.dataset.cluster});
      const response = await fetch("/api/object-storage/buckets/" + encodeURIComponent(activity.dataset.bucket) + "/activity?" + params);
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Không tải được activity");
      const trend = body.activity || {};
      if (!trend.available) {
        activityStatus.textContent = "Không đọc được activity: " + (trend.error || "RGW không cung cấp log");
        return;
      }
      const latest = trend.latest_request ? " · gần nhất " + trend.latest_request : "";
      activityStatus.textContent = trend.total + " request · " + trend.errors + " lỗi HTTP 4xx/5xx · tỷ lệ lỗi " + trend.error_rate + "%" + latest;
      activityRows.replaceChildren();
      (trend.points || []).forEach(function (point) {
        const row = document.createElement("tr");
        cell(row, point.hour); cell(row, point.requests); cell(row, point.errors);
        activityRows.appendChild(row);
      });
      activityTable.hidden = !(trend.points || []).length;
      if (!trend.points || !trend.points.length) activityStatus.textContent += " · Chưa có request nào trong đoạn log hiện tại.";
    } catch (error) {
      activityStatus.textContent = "Không tải được activity: " + error.message;
    }
  }

  function cell(row, value) {
    const td = document.createElement("td");
    td.textContent = value == null || value === "" ? "—" : String(value);
    row.appendChild(td);
  }

  function renderDetail(body) {
    const card = document.getElementById("object-detail-card");
    const content = document.getElementById("object-detail-content");
    card.hidden = false;
    document.getElementById("object-detail-key").textContent = body.key;
    document.getElementById("object-detail-status").textContent = "Ceph " + body.ceph_version + (body.version_id ? " · version " + body.version_id : "");
    content.replaceChildren();
    const table = document.createElement("table");
    const tbody = document.createElement("tbody");
    [["Dung lượng", body.size], ["Content-Type", body.content_type], ["ETag", body.etag],
      ["Last modified", body.last_modified], ["Storage class", body.storage_class],
      ["Retention", body.retention_supported ? (body.retention ? JSON.stringify(body.retention) : "Không đặt") : body.retention_unavailable_reason],
      ["Legal hold", body.retention_supported ? (body.legal_hold ? body.legal_hold.Status : "Không đặt") : body.retention_unavailable_reason],
      ["Tags", body.tags_supported ? (body.tags.length ? JSON.stringify(body.tags) : "Không có") : body.tags_unavailable_reason],
      ["User metadata", Object.keys(body.metadata).length ? JSON.stringify(body.metadata) : "Không có"]].forEach(function (pair) {
        const tr = document.createElement("tr"); cell(tr, pair[0]); cell(tr, pair[1]); tbody.appendChild(tr);
      });
    table.appendChild(tbody); content.appendChild(table);
  }

  async function loadDetail(item) {
    const endpoint = document.getElementById("object-endpoint").value.trim();
    if (!endpoint) { status.textContent = "Nhập S3 endpoint trước khi xem Object Detail."; return; }
    const params = new URLSearchParams({cluster: root.dataset.cluster, key: item.key,
      owner: root.dataset.owner, endpoint: endpoint, version_id: item.version_id || ""});
    const detailStatus = document.getElementById("object-detail-status");
    document.getElementById("object-detail-card").hidden = false; detailStatus.textContent = "Đang tải metadata…";
    try {
      const response = await fetch("/api/object-storage/buckets/" + encodeURIComponent(root.dataset.bucket) + "/object-detail?" + params);
      const body = await response.json(); if (!response.ok) throw new Error(body.detail || "Không tải được metadata");
      renderDetail(body);
    } catch (error) { detailStatus.textContent = "Lỗi: " + error.message; }
  }

  function versionPayload(action, item) {
    return {
      action: action,
      owner: root.dataset.owner,
      endpoint: document.getElementById("object-endpoint").value.trim(),
      key: item.key,
      version_id: item.version_id
    };
  }

  async function versionCall(kind, payload) {
    const url = "/api/object-storage/buckets/" + encodeURIComponent(root.dataset.bucket) +
      "/object-versions/" + kind + "?cluster=" + encodeURIComponent(root.dataset.cluster);
    const response = await fetch(url, {
      method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)
    });
    const body = await response.json();
    if (!response.ok) throw new Error(body.detail || "Không thực hiện được object version operation");
    return body;
  }

  async function previewVersionAction(action, item) {
    if (!versionCard || !item || !item.version_id) return;
    const endpoint = document.getElementById("object-endpoint").value.trim();
    const versionStatus = document.getElementById("object-version-status");
    if (!endpoint) {
      versionStatus.textContent = "Nhập S3 endpoint trước khi preview thao tác version.";
      versionCard.hidden = false;
      return;
    }
    selectedVersionItem = item;
    approvedVersionAction = null;
    document.getElementById("object-version-action").value = action;
    document.getElementById("object-version-key").value = item.key;
    document.getElementById("object-version-id").value = item.version_id;
    document.getElementById("object-version-preview").hidden = true;
    document.getElementById("object-version-confirm-wrap").hidden = true;
    versionCard.hidden = false;
    versionStatus.textContent = "Đang kiểm tra version và Object Lock…";
    try {
      const body = await versionCall("preview", versionPayload(action, item));
      approvedVersionAction = body;
      const preview = document.getElementById("object-version-preview");
      preview.hidden = false;
      preview.textContent = body.preview + "\nMức rủi ro: " + body.risk + "\n" + body.retention_warning +
        (body.allowed ? "\nĐược phép. Nhập đúng mã: " + body.confirmation_required : "\nBị chặn: " + body.blocked_reason);
      document.getElementById("object-version-confirm-wrap").hidden = !body.allowed;
      versionStatus.textContent = body.allowed ? "Preview hợp lệ; execute sẽ kiểm tra lại toàn bộ điều kiện." : "Thao tác bị chặn theo policy bảo vệ dữ liệu.";
    } catch (error) {
      versionStatus.textContent = "Lỗi preview: " + error.message;
    }
  }

  async function load(reset) {
    if (reset) marker = "";
    status.textContent = "Đang tải object…";
    next.hidden = true;
    const params = new URLSearchParams({
      cluster: root.dataset.cluster, marker: marker,
      prefix: document.getElementById("object-prefix").value,
      query: document.getElementById("object-query").value,
      sort: document.getElementById("object-sort").value,
      order: document.getElementById("object-order").value,
      page_size: "10"
    });
    try {
      const response = await fetch("/api/object-storage/buckets/" + encodeURIComponent(root.dataset.bucket) + "/objects?" + params);
      const body = await response.json();
      if (!response.ok) throw new Error(body.detail || "Không tải được object");
      rows.replaceChildren();
      body.items.forEach(function (item) {
        const tr = document.createElement("tr");
        cell(tr, item.key); cell(tr, item.size); cell(tr, item.content_type);
        cell(tr, item.last_modified); cell(tr, item.version_id);
        const action = document.createElement("td");
        const detailButton = document.createElement("button");
        detailButton.type = "button"; detailButton.className = "btn btn-ghost btn-sm"; detailButton.textContent = "Chi tiết";
        detailButton.addEventListener("click", function () { loadDetail(item); }); action.appendChild(detailButton);
        if (item.version_id && versionCard) {
          const restoreButton = document.createElement("button");
          restoreButton.type = "button"; restoreButton.className = "btn btn-ghost btn-sm"; restoreButton.textContent = "Khôi phục";
          restoreButton.addEventListener("click", function () { previewVersionAction("restore_version", item); }); action.appendChild(restoreButton);
          const deleteButton = document.createElement("button");
          deleteButton.type = "button"; deleteButton.className = "btn btn-danger-outline btn-sm"; deleteButton.textContent = "Xóa version";
          deleteButton.addEventListener("click", function () { previewVersionAction("delete_version", item); }); action.appendChild(deleteButton);
        }
        tr.appendChild(action);
        rows.appendChild(tr);
      });
      status.textContent = body.items.length ?
        "Hiển thị " + body.items.length + " object · Ceph " + body.ceph_version + " · đã quét " + body.scanned + " index entry" :
        "Không có object phù hợp trong phạm vi quét hiện tại.";
      marker = body.next_marker || "";
      next.hidden = !body.truncated || !marker;
    } catch (error) {
      rows.replaceChildren();
      status.textContent = error.message;
    }
  }

  form.addEventListener("submit", function (event) { event.preventDefault(); load(true); });
  next.addEventListener("click", function () { load(false); });
  load(true);
  loadActivity();

  if (versionCard) {
    const versionForm = document.getElementById("object-version-form");
    const versionStatus = document.getElementById("object-version-status");
    versionForm.addEventListener("submit", function (event) {
      event.preventDefault();
      if (selectedVersionItem) previewVersionAction(document.getElementById("object-version-action").value, selectedVersionItem);
      else versionStatus.textContent = "Chọn một object version trong bảng trước.";
    });
    document.getElementById("object-version-execute").addEventListener("click", async function () {
      if (!approvedVersionAction || !selectedVersionItem || !approvedVersionAction.allowed) return;
      const payload = versionPayload(approvedVersionAction.action, selectedVersionItem);
      payload.confirmation = document.getElementById("object-version-confirm").value.trim();
      versionStatus.textContent = "Đang thực hiện và ghi audit…";
      try {
        const body = await versionCall("execute", payload);
        versionStatus.textContent = "Đã hoàn tất " + body.action + " cho " + body.key + " (audit " + body.request_id + ").";
        approvedVersionAction = null;
        document.getElementById("object-version-confirm").value = "";
        document.getElementById("object-version-confirm-wrap").hidden = true;
        await load(true);
      } catch (error) {
        versionStatus.textContent = "Lỗi execute: " + error.message;
      }
    });
  }

  const presignForm = document.getElementById("object-presign-form");
  if (presignForm) {
    let approved = null;
    const presignStatus = document.getElementById("presign-status");
    function presignPayload() {
      const action = document.getElementById("presign-action").value;
      const payload = {action: action, bucket: presignForm.dataset.bucket, owner: presignForm.dataset.owner,
        key: document.getElementById("presign-key").value, version_id: document.getElementById("presign-version").value,
        endpoint: document.getElementById("presign-endpoint").value,
        access_key: document.getElementById("presign-access-key").value,
        secret_key: document.getElementById("presign-secret-key").value,
        expires_seconds: Number(document.getElementById("presign-expires").value)};
      if (action === "upload") { payload.content_type = document.getElementById("presign-content-type").value; payload.max_bytes = Number(document.getElementById("presign-max-bytes").value); }
      return payload;
    }
    async function presignCall(kind, payload) {
      const response = await fetch("/api/object-storage/objects/presign/" + kind + "?cluster=" + encodeURIComponent(presignForm.dataset.cluster),
        {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)});
      const body = await response.json(); if (!response.ok) throw new Error(body.detail || "Không tạo được URL"); return body;
    }
    presignForm.addEventListener("submit", async function (event) {
      event.preventDefault(); presignStatus.textContent = "Đang preview…";
      try { const payload = presignPayload(); approved = await presignCall("preview", payload);
        const preview = document.getElementById("presign-preview"); preview.hidden = false;
        preview.textContent = approved.action + " s3://" + approved.bucket + "/" + approved.key + "\nHết hạn: " + approved.expires_seconds + " giây" + (approved.max_bytes ? "\nGiới hạn: " + approved.max_bytes + " byte · " + approved.content_type : "");
        document.getElementById("presign-confirm-wrap").hidden = false; presignStatus.textContent = approved.credential_handling;
      } catch (error) { presignStatus.textContent = "Lỗi: " + error.message; }
    });
    document.getElementById("presign-execute").addEventListener("click", async function () {
      if (!approved) return; const payload = presignPayload(); payload.confirmation = document.getElementById("presign-confirm").value;
      try { const body = await presignCall("execute", payload); const result = document.getElementById("presign-result"); result.replaceChildren();
        const link = document.createElement("a"); link.href = body.url; link.textContent = body.action === "download" ? "Mở URL download" : "Upload endpoint"; link.rel = "noopener noreferrer"; result.appendChild(link);
        if (body.fields) { const pre = document.createElement("pre"); pre.className = "command-preview"; pre.textContent = JSON.stringify({url: body.url, fields: body.fields}, null, 2); result.appendChild(pre); }
        document.getElementById("presign-secret-key").value = ""; presignStatus.textContent = "URL đã tạo; hết hạn sau " + body.expires_seconds + " giây.";
      } catch (error) { presignStatus.textContent = "Lỗi: " + error.message; }
    });
  }
})();
