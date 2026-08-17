(function () {
  "use strict";
  const root = document.getElementById("object-browser");
  if (!root) return;
  const form = document.getElementById("object-browser-form");
  const rows = document.getElementById("object-browser-rows");
  const status = document.getElementById("object-browser-status");
  const next = document.getElementById("object-browser-next");
  let marker = "";

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
      page_size: "50"
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
        const action = document.createElement("td"); const button = document.createElement("button");
        button.type = "button"; button.className = "btn btn-ghost btn-sm"; button.textContent = "Chi tiết";
        button.addEventListener("click", function () { loadDetail(item); }); action.appendChild(button); tr.appendChild(action);
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
