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
})();
