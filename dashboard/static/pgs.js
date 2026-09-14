(() => {
  "use strict";

  const table = document.getElementById("pg-list");
  const searchInput = document.getElementById("pg-search");
  const pgIdInput = document.getElementById("pg-id-filter");
  const poolSelect = document.getElementById("pg-pool-filter");
  const resetButton = document.getElementById("pg-filter-reset");
  const result = document.getElementById("pg-filter-result");
  const empty = document.getElementById("pg-filter-empty");
  const pagination = document.getElementById("pg-pagination");
  const previousButton = document.getElementById("pg-page-prev");
  const nextButton = document.getElementById("pg-page-next");
  const pageStatus = document.getElementById("pg-page-status");
  const pageRoot = document.getElementById("pgs-page");
  const totalCount = document.getElementById("pg-total-count");
  const snapshotMeta = document.getElementById("pg-snapshot-meta");

  if (!table || !searchInput || !pgIdInput || !poolSelect || !resetButton || !result
      || !pagination || !previousButton || !nextButton || !pageStatus) return;

  let rows = Array.from(table.querySelectorAll("tbody tr"));
  const pageSize = 10;
  let currentPage = 1;
  const normalize = (value) => String(value || "").trim().toLocaleLowerCase("vi");
  let lastGeneration = null;

  // Build the Pool dropdown from the rendered PG rows themselves. This is
  // deliberately independent of Ceph's pool-list response shape: any pool
  // visible in the table must always be available as one distinct option.
  function rebuildPoolOptions() {
    const poolNames = Array.from(new Set(
      rows.map((row) => String(row.dataset.pool || "").trim()).filter((name) => name && name !== "—")
    )).sort((left, right) => left.localeCompare(right, "vi", { numeric: true }));
    const current = poolSelect.value;
    poolSelect.replaceChildren(new Option("Tất cả pool", ""));
    poolNames.forEach((poolName) => poolSelect.add(new Option(poolName, poolName)));
    poolSelect.value = poolNames.includes(current) ? current : "";
  }

  function renderRows(nextRows) {
    const tbody = table.querySelector("tbody");
    if (!tbody) return;
    tbody.replaceChildren();
    nextRows.forEach((pg) => {
      const row = document.createElement("tr");
      row.dataset.pgid = pg.pgid || "—";
      row.dataset.pool = pg.pool || "—";
      const values = [
        ["code", pg.pgid || "—"],
        ["state", pg.state || "unknown"],
        ["text", pg.pool || "—"],
        ["code", `[${(pg.acting || []).join(", ")}]`],
        ["code", `[${(pg.up || []).join(", ")}]`],
        ["code", pg.primary || "—"],
        ["text", pg.last_scrub || "—"],
        ["text", pg.last_deep_scrub || "—"],
      ];
      values.forEach(([kind, value], index) => {
        const cell = document.createElement("td");
        if (index === 0 || index === 3 || index === 4 || index === 5) {
          const content = document.createElement(kind === "code" ? "code" : "span");
          content.textContent = value;
          cell.appendChild(content);
        } else {
          cell.textContent = value;
        }
        if (index === 1) {
          const chip = document.createElement("span");
          chip.className = `pg-state-chip ${value === "active+clean" ? "is-clean" : "is-warning"}`;
          chip.textContent = value;
          cell.replaceChildren(chip);
        }
        row.appendChild(cell);
      });
      tbody.appendChild(row);
    });
    rows = Array.from(tbody.querySelectorAll("tr"));
    rebuildPoolOptions();
    render();
  }

  function matchingRows() {
    const search = normalize(searchInput.value);
    const pgId = normalize(pgIdInput.value);
    const pool = normalize(poolSelect.value);
    return rows.filter((row) => {
      const matchesSearch = !search || normalize(row.textContent).includes(search);
      const matchesPgId = !pgId || normalize(row.dataset.pgid).includes(pgId);
      const matchesPool = !pool || normalize(row.dataset.pool) === pool;
      return matchesSearch && matchesPgId && matchesPool;
    });
  }

  function render() {
    const filteredRows = matchingRows();
    const pageCount = Math.max(1, Math.ceil(filteredRows.length / pageSize));
    currentPage = Math.min(Math.max(1, currentPage), pageCount);
    const start = (currentPage - 1) * pageSize;
    const pageRows = new Set(filteredRows.slice(start, start + pageSize));

    rows.forEach((row) => { row.hidden = !pageRows.has(row); });

    const shownFrom = filteredRows.length ? start + 1 : 0;
    const shownTo = Math.min(start + pageSize, filteredRows.length);
    result.textContent = `Hiển thị ${shownFrom}-${shownTo} / ${filteredRows.length} PGs`;
    pageStatus.textContent = `Trang ${currentPage} / ${pageCount}`;
    previousButton.disabled = currentPage === 1;
    nextButton.disabled = currentPage === pageCount;
    pagination.hidden = filteredRows.length === 0;
    if (empty) empty.hidden = filteredRows.length !== 0;
    table.hidden = filteredRows.length === 0;
    const search = normalize(searchInput.value);
    const pgId = normalize(pgIdInput.value);
    const pool = normalize(poolSelect.value);
    resetButton.disabled = !search && !pgId && !pool;
  }

  function resetPageAndRender() {
    currentPage = 1;
    render();
  }

  [searchInput, pgIdInput].forEach((input) => input.addEventListener("input", resetPageAndRender));
  poolSelect.addEventListener("change", resetPageAndRender);
  previousButton.addEventListener("click", () => {
    if (currentPage > 1) currentPage -= 1;
    render();
  });
  nextButton.addEventListener("click", () => {
    currentPage += 1;
    render();
  });
  resetButton.addEventListener("click", () => {
    searchInput.value = "";
    pgIdInput.value = "";
    poolSelect.value = "";
    resetPageAndRender();
    searchInput.focus();
  });

  rebuildPoolOptions();
  render();

  function refreshSnapshot() {
    if (document.hidden || !pageRoot) return;
    const clusterId = pageRoot.dataset.clusterId;
    fetch(`/api/pgs?cluster_id=${encodeURIComponent(clusterId)}`, { credentials: "same-origin" })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json();
      })
      .then((payload) => {
        const meta = payload.meta || {};
        const generation = meta.generation == null ? null : meta.generation;
        if (meta.available !== false && Array.isArray(payload.items) && generation !== lastGeneration) {
          renderRows(payload.items);
          lastGeneration = generation;
        }
        if (totalCount) totalCount.textContent = `${payload.total ?? 0} PGs`;
        if (snapshotMeta) {
          if (meta.last_error) {
            snapshotMeta.textContent = `Snapshot error: ${meta.last_error}`;
          } else if (meta.collected_at) {
            snapshotMeta.textContent = `Snapshot generation ${meta.generation ?? 0} · ${Math.round(meta.age_seconds ?? 0)}s${meta.stale ? " · stale" : ""}`;
          } else {
            snapshotMeta.textContent = "No snapshot available";
          }
        }
      })
      .catch(() => {
        // Keep the last good table visible during a transient snapshot error.
      });
  }

  refreshSnapshot();
  const refreshTimer = window.setInterval(refreshSnapshot, 10_000);
  document.addEventListener("visibilitychange", refreshSnapshot);
})();
