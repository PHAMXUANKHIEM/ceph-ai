(() => {
  "use strict";

  const table = document.getElementById("pg-list");
  const searchInput = document.getElementById("pg-search");
  const pgIdInput = document.getElementById("pg-id-filter");
  const poolSelect = document.getElementById("pg-pool-filter");
  const resetButton = document.getElementById("pg-filter-reset");
  const result = document.getElementById("pg-filter-result");
  const empty = document.getElementById("pg-filter-empty");

  if (!table || !searchInput || !pgIdInput || !poolSelect || !resetButton || !result) return;

  const rows = Array.from(table.querySelectorAll("tbody tr"));
  const normalize = (value) => String(value || "").trim().toLocaleLowerCase("vi");

  function applyFilters() {
    const search = normalize(searchInput.value);
    const pgId = normalize(pgIdInput.value);
    const pool = normalize(poolSelect.value);
    let visible = 0;

    rows.forEach((row) => {
      const matchesSearch = !search || normalize(row.textContent).includes(search);
      const matchesPgId = !pgId || normalize(row.dataset.pgid).includes(pgId);
      const matchesPool = !pool || normalize(row.dataset.pool) === pool;
      const show = matchesSearch && matchesPgId && matchesPool;
      row.hidden = !show;
      if (show) visible += 1;
    });

    result.textContent = `Hiển thị ${visible}/${rows.length} PGs`;
    if (empty) empty.hidden = visible !== 0;
    table.hidden = visible === 0;
    resetButton.disabled = !search && !pgId && !pool;
  }

  [searchInput, pgIdInput].forEach((input) => input.addEventListener("input", applyFilters));
  poolSelect.addEventListener("change", applyFilters);
  resetButton.addEventListener("click", () => {
    searchInput.value = "";
    pgIdInput.value = "";
    poolSelect.value = "";
    applyFilters();
    searchInput.focus();
  });

  applyFilters();
})();
