(() => {
  "use strict";

  const table = document.getElementById("pg-list");
  const searchInput = document.getElementById("pg-search");
  const poolSelect = document.getElementById("pg-pool-filter");
  const resetButton = document.getElementById("pg-filter-reset");
  const result = document.getElementById("pg-filter-result");
  const empty = document.getElementById("pg-filter-empty");
  const pagination = document.getElementById("pg-pagination");
  const previousButton = document.getElementById("pg-page-prev");
  const nextButton = document.getElementById("pg-page-next");
  const pageStatus = document.getElementById("pg-page-status");
  const pageButtons = document.getElementById("pg-page-buttons");
  const pageInput = document.getElementById("pg-page-input");
  const pageRoot = document.getElementById("pgs-page");
  const totalCount = document.getElementById("pg-total-count");
  const snapshotMeta = document.getElementById("pg-snapshot-meta");
  const detailPanel = document.getElementById("pg-detail-panel");
  const detailBackdrop = document.getElementById("pg-detail-backdrop");
  const detailClose = document.getElementById("pg-detail-close");
  const detailTitle = document.getElementById("pg-detail-title");
  const detailContent = document.getElementById("pg-detail-content");
  const bootstrap = document.getElementById("pg-bootstrap-data");

  if (!table || !searchInput || !poolSelect || !resetButton || !result || !pagination
      || !previousButton || !nextButton || !pageStatus || !pageButtons) return;

  let pgData = [];
  try { pgData = bootstrap ? JSON.parse(bootstrap.textContent || "[]") : []; } catch (_error) { pgData = []; }
  let currentPage = 1;
  const pageSize = 10;
  let activeState = "";
  let lastGeneration = null;
  let selectedPgid = null;

  const normalize = (value) => String(value == null ? "" : value).trim().toLocaleLowerCase("vi");
  const stateMatches = (state, filter) => filter === "active+clean" ? state === filter : normalize(state).includes(filter);

  function formatShortTimestamp(value) {
    const raw = String(value || "—");
    if (!raw || raw === "—") return "—";
    const date = new Date(raw.replace(/(\+\d{2})(\d{2})$/, "$1:$2"));
    if (Number.isNaN(date.getTime())) return raw.length > 19 ? raw.slice(0, 16).replace("T", " ") : raw;
    const seconds = Math.round((Date.now() - date.getTime()) / 1000);
    if (seconds >= 0 && seconds < 60) return `${seconds}s ago`;
    if (seconds >= 60 && seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
    if (seconds >= 3600 && seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
    if (seconds >= 86400 && seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
    return date.toLocaleDateString("en-US", { month: "short", day: "numeric" }) + ", "
      + date.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  }

  function appendTextCell(row, value, className) {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    cell.textContent = String(value == null || value === "" ? "—" : value);
    row.appendChild(cell);
    return cell;
  }

  function appendCodeCell(row, value, className) {
    const cell = document.createElement("td");
    if (className) cell.className = className;
    const code = document.createElement("code");
    code.textContent = String(value == null || value === "" ? "—" : value);
    cell.appendChild(code);
    row.appendChild(cell);
    return cell;
  }

  function appendStateCell(row, state) {
    const cell = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = `pg-state-chip ${state === "active+clean" ? "is-clean" : "is-warning"}`;
    chip.textContent = state || "unknown";
    cell.appendChild(chip);
    row.appendChild(cell);
  }

  function appendOsdSetCell(row, pg) {
    const cell = document.createElement("td");
    cell.className = "pg-osd-cell";
    const acting = Array.isArray(pg.acting) ? pg.acting : [];
    const up = Array.isArray(pg.up) ? pg.up : [];
    const set = document.createElement("span");
    set.className = "pg-osd-set";
    acting.forEach((osd) => {
      const badge = document.createElement("span");
      badge.className = "pg-osd-pill";
      badge.textContent = String(osd);
      set.appendChild(badge);
    });
    if (!acting.length) set.textContent = "—";
    cell.appendChild(set);
    if (JSON.stringify(acting) !== JSON.stringify(up)) {
      const warning = document.createElement("span");
      warning.className = "pg-osd-warning";
      warning.textContent = "⚠";
      warning.title = `Acting: [${acting.join(", ")}] · Up: [${up.join(", ")}]`;
      warning.setAttribute("aria-label", warning.title);
      cell.appendChild(warning);
    }
    row.appendChild(cell);
  }

  function appendScrubCell(row, pg) {
    const cell = document.createElement("td");
    cell.className = "pg-scrub-cell";
    [["Scrub", pg.last_scrub], ["Deep", pg.last_deep_scrub]].forEach(([label, value]) => {
      const line = document.createElement("span");
      line.title = String(value || "—");
      const caption = document.createElement("small");
      caption.textContent = `${label}:`;
      const time = document.createElement("time");
      time.dateTime = String(value || "");
      time.textContent = formatShortTimestamp(value);
      line.append(caption, time);
      cell.appendChild(line);
    });
    row.appendChild(cell);
  }

  function renderRows(data) {
    pgData = Array.isArray(data) ? data.filter((pg) => pg && typeof pg === "object") : [];
    const tbody = table.querySelector("tbody");
    if (!tbody) return;
    tbody.replaceChildren();
    pgData.forEach((pg) => {
      const row = document.createElement("tr");
      row.dataset.pgid = pg.pgid || "—";
      row.dataset.pool = pg.pool || "—";
      row.className = "pg-data-row";
      row.tabIndex = 0;
      appendCodeCell(row, pg.pgid);
      appendStateCell(row, pg.state || "unknown");
      appendTextCell(row, pg.pool);
      appendOsdSetCell(row, pg);
      appendCodeCell(row, pg.primary, "num");
      appendScrubCell(row, pg);
      row.addEventListener("click", () => openDetail(pg));
      row.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openDetail(pg); }
      });
      tbody.appendChild(row);
    });
    rebuildPoolOptions();
    renderSummary(summarize(pgData));
    render();
  }

  function rebuildPoolOptions() {
    const names = Array.from(new Set(pgData.map((pg) => String(pg.pool || "").trim()).filter(Boolean)))
      .sort((a, b) => a.localeCompare(b, "vi", { numeric: true }));
    const current = poolSelect.value;
    poolSelect.replaceChildren(new Option("Tất cả pool", ""));
    names.forEach((name) => poolSelect.add(new Option(name, name)));
    poolSelect.value = names.includes(current) ? current : "";
  }

  function summarize(data) {
    const stateCounts = new Map();
    const poolCounts = new Map();
    const osdCounts = new Map();
    let replicas = 0;
    data.forEach((pg) => {
      const state = String(pg.state || "unknown");
      stateCounts.set(state, (stateCounts.get(state) || 0) + 1);
      const pool = String(pg.pool || "—");
      if (pool !== "—") poolCounts.set(pool, (poolCounts.get(pool) || 0) + 1);
      (Array.isArray(pg.acting) ? pg.acting : []).forEach((osd) => {
        const key = String(osd); osdCounts.set(key, (osdCounts.get(key) || 0) + 1); replicas += 1;
      });
    });
    const stateDistribution = Array.from(stateCounts, ([state, count]) => ({ state, count, percent: data.length ? +(count * 100 / data.length).toFixed(1) : 0 }))
      .sort((a, b) => b.count - a.count || a.state.localeCompare(b.state));
    const poolDistribution = Array.from(poolCounts, ([pool, count]) => ({ pool, count }))
      .sort((a, b) => b.count - a.count || a.pool.localeCompare(b.pool));
    return {
      total: data.length,
      state_distribution: stateDistribution,
      pool_distribution: poolDistribution,
      quick_filters: {
        "active+clean": data.filter((pg) => String(pg.state || "") === "active+clean").length,
        degraded: data.filter((pg) => normalize(pg.state).includes("degraded")).length,
        stale: data.filter((pg) => normalize(pg.state).includes("stale")).length,
        undersized: data.filter((pg) => normalize(pg.state).includes("undersized")).length,
      },
      osd_count: osdCounts.size,
      pgs_per_osd: osdCounts.size ? +(replicas / osdCounts.size).toFixed(1) : null,
    };
  }

  function renderSummary(summary) {
    const total = Number(summary.total || 0);
    const totalElement = document.getElementById("pg-summary-total");
    const headerTotal = document.getElementById("pg-total-count");
    if (totalElement) totalElement.textContent = String(total);
    if (headerTotal) headerTotal.textContent = String(total);
    const headerHealth = document.getElementById("pg-header-health");
    if (headerHealth) headerHealth.textContent = `${(summary.quick_filters && summary.quick_filters["active+clean"]) || 0} active+clean`;
    const stateLabel = document.getElementById("pg-state-summary-label");
    if (stateLabel) stateLabel.textContent = `${(summary.state_distribution || []).length} states`;
    const stateBar = document.getElementById("pg-state-bar");
    const stateDonut = document.getElementById("pg-state-donut");
    const cleanCount = Number(summary.quick_filters && summary.quick_filters["active+clean"] || 0);
    if (stateDonut) {
      stateDonut.style.setProperty("--pg-clean-pct", `${total ? (cleanCount * 100 / total).toFixed(1) : 0}%`);
      stateDonut.title = `${cleanCount} / ${total} active+clean`;
    }
    if (stateBar) {
      stateBar.replaceChildren();
      (summary.state_distribution || []).forEach((item) => {
        const segment = document.createElement("span");
        segment.className = `pg-state-segment ${item.state === "active+clean" ? "is-clean" : "is-warning"}`;
        segment.style.width = `${item.percent || 0}%`;
        segment.title = `${item.state}: ${item.count}`;
        stateBar.appendChild(segment);
      });
    }
    const legend = document.getElementById("pg-state-legend");
    if (legend) {
      legend.replaceChildren();
      (summary.state_distribution || []).forEach((item) => {
        const itemElement = document.createElement("span");
        const dot = document.createElement("i");
        dot.className = item.state === "active+clean" ? "is-clean" : "is-warning";
        const label = document.createTextNode(`${item.state} `);
        const count = document.createElement("b"); count.textContent = String(item.count);
        itemElement.append(dot, label, count); legend.appendChild(itemElement);
      });
    }
    const poolList = document.getElementById("pg-pool-summary");
    if (poolList) {
      poolList.replaceChildren();
      const pools = summary.pool_distribution || [];
      const maximum = pools.length && pools[0].count ? pools[0].count : 1;
      pools.slice(0, 5).forEach((item) => {
        const line = document.createElement("div"); line.className = "pg-pool-item";
        const name = document.createElement("span"); name.textContent = item.pool; name.title = item.pool;
        const bar = document.createElement("div"); bar.className = "pg-mini-bar";
        const fill = document.createElement("i"); fill.style.width = `${item.count * 100 / maximum}%`; bar.appendChild(fill);
        const count = document.createElement("b"); count.textContent = String(item.count);
        line.append(name, bar, count); poolList.appendChild(line);
      });
      if (pools.length > 5) { const more = document.createElement("small"); more.className = "pg-more-pools"; more.textContent = `+${pools.length - 5} pools khác`; poolList.appendChild(more); }
      const poolCount = document.getElementById("pg-pool-count"); if (poolCount) poolCount.textContent = `${pools.length} pools`;
    }
    const osdAverage = document.getElementById("pg-summary-osd-average"); if (osdAverage) osdAverage.textContent = summary.pgs_per_osd == null ? "—" : String(summary.pgs_per_osd);
    const osdCount = document.getElementById("pg-summary-osd-count"); if (osdCount) osdCount.textContent = String(summary.osd_count || 0);
    document.querySelectorAll(".pg-quick-filter").forEach((button) => {
      const count = Number((summary.quick_filters && summary.quick_filters[button.dataset.pgState]) || 0);
      const value = button.querySelector("b"); if (value) value.textContent = String(count);
      button.disabled = count === 0;
      button.classList.toggle("is-active", activeState === button.dataset.pgState);
    });
  }

  function matchingData() {
    const searchTerms = normalize(searchInput.value).split(/\s+/).filter(Boolean);
    const pool = normalize(poolSelect.value);
    return pgData.filter((pg) => {
      const haystack = normalize([pg.pgid, pg.state, pg.pool, pg.primary, ...(pg.acting || []), ...(pg.up || []), pg.last_scrub, pg.last_deep_scrub].join(" "));
      return searchTerms.every((term) => haystack.includes(term))
        && (!pool || normalize(pg.pool) === pool)
        && (!activeState || stateMatches(pg.state, activeState));
    });
  }

  function render() {
    const data = matchingData();
    const size = pageSize;
    const pageCount = Math.max(1, Math.ceil(data.length / size));
    currentPage = Math.min(Math.max(1, currentPage), pageCount);
    const start = (currentPage - 1) * size;
    const visibleIds = new Set(data.slice(start, start + size).map((pg) => String(pg.pgid)));
    table.querySelectorAll("tbody tr").forEach((row) => { row.hidden = !visibleIds.has(String(row.dataset.pgid)); });
    const shownFrom = data.length ? start + 1 : 0;
    const shownTo = Math.min(start + size, data.length);
    const summary = `Hiển thị ${shownFrom}–${shownTo} / ${data.length} mục`;
    result.textContent = summary;
    const paginationSummary = document.getElementById("pg-filter-result-pagination");
    if (paginationSummary) { paginationSummary.textContent = summary; paginationSummary.dataset.mobileSummary = `${shownFrom}–${shownTo} / ${data.length}`; }
    pageStatus.textContent = `Trang ${currentPage} / ${pageCount}`;
    previousButton.disabled = currentPage === 1;
    nextButton.disabled = currentPage === pageCount;
    if (window.DashboardPagination) window.DashboardPagination.renderPages(pageButtons, currentPage, pageCount, (page) => { currentPage = page; render(); });
    pagination.hidden = data.length === 0;
    table.hidden = data.length === 0;
    if (empty) empty.hidden = data.length !== 0;
    resetButton.disabled = !searchInput.value.trim() && !poolSelect.value && !activeState;
  }

  function closeDetail() {
    selectedPgid = null;
    if (detailPanel) { detailPanel.classList.remove("is-open"); detailPanel.hidden = true; }
    if (detailBackdrop) detailBackdrop.hidden = true;
  }

  function appendDetailField(parent, label, value, formatter) {
    const wrapper = document.createElement("div"); wrapper.className = "pg-detail-field";
    const caption = document.createElement("dt"); caption.textContent = label;
    const content = document.createElement("dd");
    if (formatter) formatter(content, value); else content.textContent = String(value == null || value === "" ? "—" : value);
    wrapper.append(caption, content); parent.appendChild(wrapper);
  }

  function renderDetailOsds(parent, value) {
    const set = document.createElement("span"); set.className = "pg-osd-set";
    (Array.isArray(value) ? value : []).forEach((osd) => { const badge = document.createElement("span"); badge.className = "pg-osd-pill"; badge.textContent = String(osd); set.appendChild(badge); });
    if (!set.childNodes.length) set.textContent = "—";
    parent.appendChild(set);
  }

  function openDetail(pg) {
    if (!detailPanel || !detailContent) return;
    selectedPgid = String(pg.pgid || "");
    detailTitle.textContent = pg.pgid || "Placement Group";
    detailContent.replaceChildren();
    const state = document.createElement("span"); state.className = `pg-state-chip ${pg.state === "active+clean" ? "is-clean" : "is-warning"}`; state.textContent = pg.state || "unknown";
    detailContent.appendChild(state);
    const overview = document.createElement("dl"); overview.className = "pg-detail-grid";
    appendDetailField(overview, "Pool", pg.pool);
    appendDetailField(overview, "State", pg.state);
    appendDetailField(overview, "Primary OSD", pg.primary);
    appendDetailField(overview, "OSD set · Acting", pg.acting, renderDetailOsds);
    appendDetailField(overview, "OSD set · Up", pg.up, renderDetailOsds);
    appendDetailField(overview, "Last scrub", pg.last_scrub);
    appendDetailField(overview, "Last deep scrub", pg.last_deep_scrub);
    if (pg.objects != null) appendDetailField(overview, "Objects", Number(pg.objects).toLocaleString());
    if (pg.bytes != null) appendDetailField(overview, "Bytes", Number(pg.bytes).toLocaleString());
    detailContent.appendChild(overview);
    const acting = JSON.stringify(pg.acting || []); const up = JSON.stringify(pg.up || []);
    if (acting !== up) { const warning = document.createElement("p"); warning.className = "pg-detail-warning"; warning.textContent = "Acting set khác Up set — PG có thể đang recovery hoặc remapping."; detailContent.appendChild(warning); }
    detailPanel.hidden = false; if (detailBackdrop) detailBackdrop.hidden = false;
    requestAnimationFrame(() => detailPanel.classList.add("is-open"));
  }

  [searchInput].forEach((input) => input.addEventListener("input", () => { currentPage = 1; render(); }));
  poolSelect.addEventListener("change", () => { currentPage = 1; render(); });
  previousButton.addEventListener("click", () => { currentPage -= 1; render(); });
  nextButton.addEventListener("click", () => { currentPage += 1; render(); });
  resetButton.addEventListener("click", () => { searchInput.value = ""; poolSelect.value = ""; activeState = ""; document.querySelectorAll(".pg-quick-filter").forEach((button) => button.classList.remove("is-active")); currentPage = 1; render(); searchInput.focus(); });
  document.querySelectorAll(".pg-quick-filter").forEach((button) => button.addEventListener("click", () => { activeState = activeState === button.dataset.pgState ? "" : button.dataset.pgState; currentPage = 1; renderSummary(summarize(pgData)); render(); }));
  if (detailClose) detailClose.addEventListener("click", closeDetail);
  if (detailBackdrop) detailBackdrop.addEventListener("click", closeDetail);
  document.addEventListener("keydown", (event) => { if (event.key === "Escape" && detailPanel && !detailPanel.hidden) closeDetail(); });

  renderRows(pgData);

  function refreshSnapshot() {
    if (document.hidden || !pageRoot) return;
    const clusterId = pageRoot.dataset.clusterId;
    fetch(`/api/pgs?cluster_id=${encodeURIComponent(clusterId)}`, { credentials: "same-origin" })
      .then((response) => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); })
      .then((payload) => {
        const meta = payload.meta || {};
        const generation = meta.generation == null ? null : meta.generation;
        if (meta.available !== false && Array.isArray(payload.items) && generation !== lastGeneration) { renderRows(payload.items); lastGeneration = generation; }
        if (meta.available !== false && Array.isArray(payload.items) && generation === lastGeneration) renderSummary(payload.summary || summarize(pgData));
        if (totalCount && payload.total != null) totalCount.textContent = String(payload.total);
        if (snapshotMeta) snapshotMeta.textContent = meta.last_error ? `Snapshot error: ${meta.last_error}` : meta.collected_at ? `Snapshot ${meta.generation == null ? 0 : meta.generation} · ${Math.round(meta.age_seconds == null ? 0 : meta.age_seconds)}s${meta.stale ? " · stale" : ""}` : "No snapshot available";
      }).catch(() => { /* Keep the last good table during a transient snapshot error. */ });
  }
  refreshSnapshot();
  const refreshTimer = window.setInterval(refreshSnapshot, 10000);
  document.addEventListener("visibilitychange", refreshSnapshot);
})();
