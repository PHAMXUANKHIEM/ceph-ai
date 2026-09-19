(function () {
  "use strict";
  var panel = document.getElementById("volume-inventory-panel");
  if (!panel) return;
  var pool = panel.dataset.pool, selectedImage = panel.dataset.image;
  var form = document.getElementById("volume-inventory-filter");
  var search = document.getElementById("volume-inventory-search");
  var tbody = document.querySelector("#volume-inventory-table tbody");
  var error = document.getElementById("volume-inventory-error");
  var freshness = document.getElementById("volume-inventory-freshness");
  var pager = document.getElementById("volume-inventory-pagination");
  var prev = document.getElementById("volume-inventory-prev"), next = document.getElementById("volume-inventory-next");
  var pageStatus = document.getElementById("volume-inventory-page-status"), pageButtons = document.getElementById("volume-inventory-page-buttons");
  var pageSummary = document.getElementById("volume-inventory-summary"), sortMenu = document.getElementById("volume-inventory-sort-menu");
  var sortLabel = document.getElementById("volume-inventory-sort-label"), overview = document.getElementById("volume-pool-overview");
  var overviewError = document.getElementById("volume-pool-overview-error"), healthChecks = document.getElementById("volume-pool-health-checks");
  var dependencyStatus = document.getElementById("volume-dependency-status"), dependencyList = document.getElementById("volume-dependency-list"), dependencyRefresh = document.getElementById("volume-dependency-refresh");
  var protectionStatus = document.getElementById("volume-protection-status"), protectionList = document.getElementById("volume-protection-list"), protectionRefresh = document.getElementById("volume-protection-refresh");
  var state = { page: 1, pages: 1, loading: false, sort: "name", order: "asc" }, PAGE_SIZE = 10;

  function bytes(value) { var n = Number(value || 0), units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"], i = 0; while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; } return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + units[i]; }
  function percent(used, provisioned, explicit) { var value = Number(explicit), denominator = Number(provisioned || 0); if (!Number.isFinite(value)) value = denominator > 0 ? Number(used || 0) * 100 / denominator : 0; return Math.max(0, Math.min(100, value)).toFixed(1) + "%"; }
  function requestJson(url, options) {
    var cluster = new URLSearchParams(window.location.search).get("cluster");
    if (cluster) { var scoped = new URL(url, window.location.origin); scoped.searchParams.set("cluster", cluster); url = scoped.pathname + scoped.search; }
    var requestOptions = options || {}; requestOptions.credentials = "same-origin";
    return fetch(url, requestOptions).then(function (response) {
      if (response.redirected && response.url.indexOf("/login") !== -1) { window.location.reload(); throw new Error("unauthenticated"); }
      if (!response.ok) return response.json().catch(function () { return {}; }).then(function (body) { throw new Error(body.detail || "HTTP " + response.status); });
      return response.json();
    });
  }
  function detailUrl(image) { var url = "/volumes/" + encodeURIComponent(pool) + "/" + encodeURIComponent(image), cluster = new URLSearchParams(window.location.search).get("cluster"); return cluster ? url + "?cluster=" + encodeURIComponent(cluster) : url; }
  function truncated(value) { var text = String(value || ""); return text.length > 18 ? text.slice(0, 10) + "…" + text.slice(-6) : text; }
  function cell(row, value, className) { var td = document.createElement("td"); if (className) td.className = className; if (value instanceof Node) td.appendChild(value); else td.textContent = value; row.appendChild(td); return td; }

  function renderRows(data) {
    tbody.innerHTML = "";
    if (!data.items.length) { var empty = document.createElement("tr"), message = cell(empty, "Không có Volume phù hợp trong pool này.", "hint"); message.colSpan = 8; tbody.appendChild(empty); }
    data.items.forEach(function (item) {
      var row = document.createElement("tr"); row.className = "volume-inventory-row"; row.tabIndex = 0; row.setAttribute("aria-label", "Mở chi tiết " + item.name);
      var open = function () { window.location.href = detailUrl(item.name); }; row.addEventListener("click", open); row.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); } });
      var nameCell = document.createElement("div"); nameCell.className = "volume-name-cell";
      var friendly = item.display_name || (/^(?:volume-)?[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/i.test(item.name) ? "" : item.name);
      if (friendly) { var name = document.createElement("strong"); name.textContent = friendly; nameCell.appendChild(name); var secondary = document.createElement("span"); secondary.className = "volume-id-secondary"; secondary.textContent = truncated(item.name); secondary.title = item.name; nameCell.appendChild(secondary); }
      else { var uuid = document.createElement("span"); uuid.className = "volume-id-truncated"; uuid.textContent = truncated(item.name); uuid.title = "Click để xem chi tiết · " + item.name; nameCell.appendChild(uuid); }
      cell(row, nameCell); var imageCell = cell(row, truncated(item.image_id), "volume-image-id-column"); imageCell.title = item.image_id || "";
      cell(row, bytes(item.used_size), "num"); cell(row, bytes(item.provisioned_size), "volume-logic-column num");
      var usage = document.createElement("div"); usage.className = "volume-usage-cell"; var usageLabel = document.createElement("span"); usageLabel.className = "volume-usage-label"; usageLabel.textContent = percent(item.used_size, item.provisioned_size, item.used_percent); usage.appendChild(usageLabel);
      var bar = document.createElement("div"); bar.className = "volume-usage-bar"; var fill = document.createElement("span"); fill.style.width = percent(item.used_size, item.provisioned_size, item.used_percent); bar.appendChild(fill); usage.appendChild(bar); cell(row, usage);
      cell(row, String(item.snapshot_count || 0), "num"); var status = document.createElement("span"), attachmentState = String(item.attachment_state || "unknown"); status.className = "volume-status " + (attachmentState === "attached" ? "active" : "idle"); status.innerHTML = '<i class="volume-status-dot"></i>' + (attachmentState === "attached" ? "Attached" : (attachmentState === "idle" ? "Idle" : "Unknown")); status.title = attachmentState === "unknown" ? "Không xác định được từ rbd status; mở chi tiết để kiểm tra" : (String(item.watcher_count || 0) + " watcher"); cell(row, status); cell(row, "›", "volume-row-chevron"); tbody.appendChild(row);
    });
    state.page = data.page; state.pages = data.pages; pager.hidden = data.total <= data.page_size;
    var first = data.total ? ((data.page - 1) * data.page_size + 1) : 0, last = Math.min(data.page * data.page_size, data.total); pageSummary.textContent = "Hiển thị " + first + "–" + last + " / " + data.total + " mục"; pageSummary.dataset.mobileSummary = first + "–" + last + " / " + data.total; pageStatus.textContent = "Trang " + data.page + " / " + data.pages; prev.disabled = data.page <= 1; next.disabled = data.page >= data.pages;
    if (window.DashboardPagination) window.DashboardPagination.renderPages(pageButtons, data.page, data.pages, function (page) { state.page = page; loadInventory(); });
    document.getElementById("volume-page-total").textContent = String(data.summary.image_count || data.total || 0); document.getElementById("volume-page-updated").textContent = "Cập nhật: " + new Date(data.collected_at).toLocaleTimeString("vi-VN"); freshness.textContent = "Cập nhật live: " + new Date(data.collected_at).toLocaleString("vi-VN") + " · Đã dùng " + bytes(data.summary.used_size) + " / " + bytes(data.summary.provisioned_size);
    var activeTab = document.querySelector('.volumes-pool-tab[data-pool="' + pool.replace(/"/g, '\\"') + '"] [data-pool-count]'); if (activeTab) activeTab.textContent = "(" + data.total + ")";
  }
  function loadInventory() { if (state.loading) return; state.loading = true; error.hidden = true; var params = new URLSearchParams({ search: search.value.trim(), sort: state.sort, order: state.order, page: String(state.page), page_size: String(PAGE_SIZE) }); requestJson("/api/volumes/" + encodeURIComponent(pool) + "/inventory?" + params.toString()).then(renderRows).catch(function (exc) { if (exc.message === "unauthenticated") return; error.textContent = exc.message; error.hidden = false; freshness.textContent = "Không lấy được dữ liệu live"; }).finally(function () { state.loading = false; }); }
  function setOverview(field, value) { var target = overview.querySelector('[data-field="' + field + '"]'); if (!target) return; if (field === "health") { var warn = /warn|near|full|error|fail/i.test(String(value)); target.innerHTML = '<span class="volume-health-value"><i class="volume-health-dot' + (warn ? ' warn' : '') + '"></i>' + String(value || "—") + '</span>'; } else target.textContent = value; }
  function loadOverview() { requestJson("/api/volumes/" + encodeURIComponent(pool) + "/inventory-overview").then(function (data) { setOverview("type", data.pool_type || data.type || "—"); setOverview("durability", data.durability || (data.replica_size ? "Replica " + data.replica_size + ", min " + (data.min_size || "—") : (data.erasure_code_profile ? "EC " + data.erasure_code_profile : "—"))); setOverview("pg", data.pg_num ? data.pg_num + " / " + (data.pgp_num || data.pg_num) : "—"); setOverview("physical", bytes(data.physical_used_bytes || data.bytes_used)); setOverview("rbd", data.rbd_enabled === false ? "Disabled" : "Enabled"); setOverview("health", data.health || "ok"); healthChecks.textContent = (data.health_checks || []).map(function (item) { return item.summary || item.code || String(item); }).join(" · "); }).catch(function (exc) { overviewError.textContent = "Không lấy được tổng quan Pool: " + exc.message; overviewError.hidden = false; }); }
  function loadPoolCounts() { Array.prototype.forEach.call(document.querySelectorAll(".volumes-pool-tab"), function (tab) { var tabPool = tab.dataset.pool, target = tab.querySelector("[data-pool-count]"); requestJson("/api/volumes/" + encodeURIComponent(tabPool) + "/inventory?page=1&page_size=1").then(function (data) { target.textContent = "(" + data.total + ")"; }).catch(function () { target.textContent = ""; }); }); }
  function renderDependencyInsights(data) {
    if (!dependencyList || !dependencyStatus) return;
    dependencyList.innerHTML = "";
    var items = data.insights || [];
    dependencyStatus.textContent = items.length ? (items.length + " cảnh báo dependency · đã quét " + (data.queried_images || 0) + " volume") : "Không phát hiện snapshot/clone dependency trong phạm vi đã quét.";
    if (data.stale) dependencyStatus.textContent += " · dữ liệu cache đang cũ, sẽ refresh nền.";
    if (!items.length) return;
    items.forEach(function (item) {
      var card = document.createElement("article"); card.className = "volume-dependency-item";
      var title = document.createElement("div"); title.className = "volume-dependency-item-title";
      var name = document.createElement("strong"); name.textContent = (item.pool || pool) + "/" + (item.image || "—"); title.appendChild(name);
      var kind = document.createElement("span"); kind.className = "volume-dependency-kind " + (item.kind === "INSUFFICIENT_EVIDENCE" ? "warning" : "danger"); kind.textContent = item.kind; title.appendChild(kind); card.appendChild(title);
      var reason = document.createElement("p"); reason.textContent = item.reason || "—"; card.appendChild(reason);
      var recommendation = document.createElement("small"); recommendation.textContent = item.recommendation || "Không có recommendation."; card.appendChild(recommendation);
      if (item.parent || (item.children || []).length) { var dependency = document.createElement("small"); dependency.className = "hint"; dependency.textContent = item.parent ? "Parent: " + item.parent : "Children: " + item.children.length; card.appendChild(dependency); }
      dependencyList.appendChild(card);
    });
  }
  function loadDependencyInsights() { if (!dependencyStatus) return; dependencyStatus.textContent = "Đang kiểm tra dependency…"; requestJson("/api/volumes/" + encodeURIComponent(pool) + "/snapshot-clone-insights?max_images=20").then(renderDependencyInsights).catch(function (exc) { dependencyStatus.textContent = "Không đọc được snapshot/clone dependency: " + exc.message; }); }
  function renderProtectionInsights(data) {
    if (!protectionList || !protectionStatus) return;
    protectionList.innerHTML = "";
    var items = data.insights || [];
    protectionStatus.textContent = items.length ? (items.length + " khoảng trống bảo vệ · đã kiểm tra " + (data.queried_images || 0) + " volume") : "Không phát hiện khoảng trống backup/recovery trong phạm vi đã kiểm tra.";
    if (data.stale) protectionStatus.textContent += " · inventory cache đang cũ, sẽ refresh nền.";
    items.forEach(function (item) {
      var card = document.createElement("article"); card.className = "volume-protection-item";
      var title = document.createElement("div"); title.className = "volume-dependency-item-title";
      var name = document.createElement("strong"); name.textContent = item.scope === "cluster" ? "Cluster-level restore drill" : ((item.pool || pool) + "/" + (item.image || "—")); title.appendChild(name);
      var kind = document.createElement("span"); kind.className = "volume-protection-kind " + (item.kind === "INSUFFICIENT_EVIDENCE" ? "warning" : "danger"); kind.textContent = item.kind; title.appendChild(kind); card.appendChild(title);
      var reason = document.createElement("p"); reason.textContent = item.reason || "—"; card.appendChild(reason);
      var recommendation = document.createElement("small"); recommendation.textContent = item.recommendation || "Không có recommendation."; card.appendChild(recommendation);
      var evidence = document.createElement("small"); evidence.className = "hint"; evidence.textContent = item.last_success_at ? "Backup thành công: " + new Date(item.last_success_at).toLocaleString("vi-VN") : "Chưa có backup thành công được ghi nhận"; card.appendChild(evidence);
      if (item.snapshot_policy_enabled) { var policy = document.createElement("small"); policy.className = "hint"; policy.textContent = "Có snapshot policy; policy không thay thế backup độc lập."; card.appendChild(policy); }
      if ((item.evidence_gaps || []).length) { var gaps = document.createElement("small"); gaps.className = "hint"; gaps.textContent = "Giới hạn bằng chứng: " + item.evidence_gaps.join(" · "); card.appendChild(gaps); }
      protectionList.appendChild(card);
    });
  }
  function loadProtectionInsights() { if (!protectionStatus) return; protectionStatus.textContent = "Đang kiểm tra trạng thái bảo vệ…"; requestJson("/api/volumes/" + encodeURIComponent(pool) + "/protection-insights?max_images=50").then(renderProtectionInsights).catch(function (exc) { protectionStatus.textContent = "Không đọc được lịch sử backup/protection: " + exc.message; }); }
  form.addEventListener("submit", function (event) { event.preventDefault(); state.page = 1; loadInventory(); });
  var searchTimer; search.addEventListener("input", function () { clearTimeout(searchTimer); searchTimer = setTimeout(function () { state.page = 1; loadInventory(); }, 260); });
  Array.prototype.forEach.call(sortMenu.querySelectorAll("[data-sort]"), function (button) { button.addEventListener("click", function () { state.sort = button.dataset.sort; state.order = button.dataset.order; sortLabel.textContent = button.textContent; sortMenu.open = false; state.page = 1; loadInventory(); }); });
  prev.addEventListener("click", function () { if (state.page > 1) { state.page -= 1; loadInventory(); } }); next.addEventListener("click", function () { if (state.page < state.pages) { state.page += 1; loadInventory(); } });
  if (selectedImage) { window.location.replace(detailUrl(selectedImage)); return; }
  if (dependencyRefresh) dependencyRefresh.addEventListener("click", loadDependencyInsights);
  if (protectionRefresh) protectionRefresh.addEventListener("click", loadProtectionInsights);
  loadOverview(); loadInventory(); loadPoolCounts(); loadDependencyInsights(); loadProtectionInsights();
}());
