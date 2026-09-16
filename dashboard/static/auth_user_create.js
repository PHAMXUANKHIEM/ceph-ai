(() => {
  "use strict";

  const table = document.getElementById("auth-user-table");
  const search = document.getElementById("auth-user-search");
  const type = document.getElementById("auth-user-type");
  const result = document.getElementById("auth-user-result");
  const empty = document.getElementById("auth-user-empty");
  const pageStatus = document.getElementById("auth-user-page-status");
  const pageSizeSelect = document.getElementById("auth-user-page-size");
  const previous = document.getElementById("auth-user-prev");
  const next = document.getElementById("auth-user-next");
  if (!table || !search || !type || !result || !pageStatus || !pageSizeSelect || !previous || !next) return;

  const rows = Array.from(table.querySelectorAll("tbody tr"));
  let page = 1;

  function render() {
    const query = search.value.trim().toLocaleLowerCase("vi");
    const kind = type.value;
    const filtered = rows.filter((row) => {
      const matchesText = !query || String(row.dataset.authUser || "").includes(query);
      const matchesKind = !kind || row.dataset.authKind === kind;
      return matchesText && matchesKind;
    });
    const pageSize = Number(pageSizeSelect.value) || 15;
    const pages = Math.max(1, Math.ceil(filtered.length / pageSize));
    page = Math.min(Math.max(1, page), pages);
    const visible = new Set(filtered.slice((page - 1) * pageSize, page * pageSize));
    rows.forEach((row) => { row.hidden = !visible.has(row); });
    const from = filtered.length ? (page - 1) * pageSize + 1 : 0;
    const to = Math.min(page * pageSize, filtered.length);
    result.textContent = `Hiển thị ${from}-${to} / ${filtered.length} users`;
    pageStatus.textContent = `Trang ${page} / ${pages}`;
    previous.disabled = page <= 1;
    next.disabled = page >= pages;
    if (empty) empty.hidden = filtered.length !== 0;
  }

  [search, type].forEach((control) => control.addEventListener("input", () => { page = 1; render(); }));
  pageSizeSelect.addEventListener("change", () => { page = 1; render(); });
  previous.addEventListener("click", () => { page -= 1; render(); });
  next.addEventListener("click", () => { page += 1; render(); });
  document.querySelectorAll("[data-auth-expand]").forEach((button) => button.addEventListener("click", () => {
    const list = button.previousElementSibling;
    if (!list) return;
    const expanded = list.classList.toggle("is-expanded");
    button.textContent = expanded ? "Thu gọn" : "Xem thêm";
  }));
  document.querySelectorAll("[data-auth-delete]").forEach((button) => button.addEventListener("click", (event) => {
    const entity = button.getAttribute("aria-label") || "user này";
    if (!window.confirm(`Xóa ${entity}? Thao tác này không thể hoàn tác.`)) event.preventDefault();
  }));
  render();
})();
