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
