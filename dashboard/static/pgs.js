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

  if (!table || !searchInput || !pgIdInput || !poolSelect || !resetButton || !result
      || !pagination || !previousButton || !nextButton || !pageStatus) return;

  const rows = Array.from(table.querySelectorAll("tbody tr"));
  const pageSize = 10;
  let currentPage = 1;
  const normalize = (value) => String(value || "").trim().toLocaleLowerCase("vi");

  // Build the Pool dropdown from the rendered PG rows themselves. This is
  // deliberately independent of Ceph's pool-list response shape: any pool
  // visible in the table must always be available as one distinct option.
  const poolNames = Array.from(new Set(
    rows.map((row) => String(row.dataset.pool || "").trim()).filter((name) => name && name !== "—")
  )).sort((left, right) => left.localeCompare(right, "vi", { numeric: true }));
  poolSelect.replaceChildren(new Option("Tất cả pool", ""));
  poolNames.forEach((poolName) => poolSelect.add(new Option(poolName, poolName)));

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

  render();
})();
