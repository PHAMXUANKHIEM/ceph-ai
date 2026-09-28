// Read-only Ceph feature status on the Disk Risk page (autonomy plan WP4).
(function () {
  const card = document.getElementById("ceph-features");
  if (!card) return;
  const status = document.getElementById("ceph-features-status");
  const table = card.querySelector(".ceph-features-table");
  const body = table.querySelector("tbody");
  const LABELS = { ok: "Ổn", info: "Thông tin", warn: "Cần xem", unavailable: "Không rõ" };

  function cell(text, className) {
    const td = document.createElement("td");
    td.textContent = text || "";
    if (className) td.className = className;
    return td;
  }

  function render(report) {
    body.replaceChildren();
    for (const feature of report.features || []) {
      const row = document.createElement("tr");
      row.dataset.feature = feature.id;
      row.append(cell(feature.title), cell(LABELS[feature.state] || feature.state, `ceph-feature-${feature.state}`),
        cell(feature.summary), cell(feature.recommendation || "—"));
      body.append(row);
    }
    const devices = report.devices || {};
    const errors = Object.keys(report.errors || {}).length;
    table.hidden = !(report.features || []).length;
    status.textContent = `${devices.total || 0} device · ${devices.with_prediction || 0} có dự đoán tuổi thọ · `
      + `SMART ${devices.with_smart || 0}/${devices.sampled_for_smart || 0} device lấy mẫu`
      + (devices.smartctl_failed ? ` (smartctl lỗi ${devices.smartctl_failed})` : "")
      + (errors ? ` · ${errors} lệnh lỗi` : "") + ` · đọc lúc ${report.collected_at || "?"}`;
  }

  async function load(refresh) {
    status.textContent = "Đang đọc trạng thái từ cluster…";
    const params = new URLSearchParams({ cluster_id: card.dataset.clusterId || "" });
    if (refresh) params.set("refresh", "1");
    try {
      const response = await fetch(`/api/ceph-features?${params}`, { credentials: "same-origin" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      render(await response.json());
    } catch (error) {
      table.hidden = true;
      status.textContent = `Không đọc được trạng thái: ${error.message}`;
    }
  }

  document.getElementById("ceph-features-refresh").addEventListener("click", () => load(true));
  load(false);
})();
