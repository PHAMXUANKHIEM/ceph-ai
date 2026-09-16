(() => {
  "use strict";

  const table = document.getElementById("trash-entry-list");
  const filterInput = document.getElementById("trash-id-filter");
  const resetButton = document.getElementById("trash-filter-reset");
  const result = document.getElementById("trash-filter-result");
  const empty = document.getElementById("trash-filter-empty");
  const pagination = document.getElementById("trash-pagination");
  const previousButton = document.getElementById("trash-page-prev");
  const nextButton = document.getElementById("trash-page-next");
  const pageStatus = document.getElementById("trash-page-status");
  const selectAll = document.getElementById("trash-select-all");
  const restoreSelected = document.getElementById("trash-restore-selected");
  const deleteSelected = document.getElementById("trash-delete-selected");

  async function copyId(button) {
    const value = button.dataset.copyValue || "";
    try {
      await navigator.clipboard.writeText(value);
    } catch (_error) {
      const text = document.createElement("textarea");
      text.value = value;
      text.setAttribute("readonly", "");
      text.style.position = "fixed";
      text.style.opacity = "0";
      document.body.appendChild(text);
      text.select();
      document.execCommand("copy");
      text.remove();
    }
    const original = button.innerHTML;
    button.innerHTML = "<code>Đã sao chép</code>";
    window.setTimeout(() => { button.innerHTML = original; }, 1200);
  }

  function makeDialog({ target, expected, title, description, label }) {
    const dialog = document.createElement("dialog");
    dialog.className = "trash-confirm-dialog";
    dialog.setAttribute("aria-labelledby", "trash-confirm-title");
    dialog.innerHTML = `
      <div class="trash-confirm-bar">
        <div class="trash-confirm-icon" aria-hidden="true">!</div>
        <div class="trash-confirm-content">
          <h2 id="trash-confirm-title">${title}</h2>
          <p>${description}</p>
          <strong class="trash-confirm-target"></strong>
          <label class="trash-confirm-input-label" for="trash-confirm-input">${label}</label>
          <input id="trash-confirm-input" class="trash-confirm-input" type="text" autocomplete="off" spellcheck="false">
          <div class="trash-confirm-actions">
            <button type="button" class="btn btn-ghost btn-sm" data-trash-confirm-cancel>Huỷ</button>
            <button type="button" class="btn btn-danger btn-sm" data-trash-confirm-submit disabled>Xác nhận xoá</button>
          </div>
        </div>
      </div>`;
    dialog.querySelector(".trash-confirm-target").textContent = target;
    document.body.appendChild(dialog);
    return { dialog, input: dialog.querySelector(".trash-confirm-input"), cancel: dialog.querySelector("[data-trash-confirm-cancel]"), confirm: dialog.querySelector("[data-trash-confirm-submit]"), expected };
  }

  function openTrashConfirmation(form) {
    if (form.dataset.confirmOpen === "true") return;
    form.dataset.confirmOpen = "true";
    const target = form.dataset.confirmTarget || "Trash";
    const isPoolPurge = form.classList.contains("trash-force-purge-form");
    const expected = isPoolPurge ? "OK" : target;
    const modal = makeDialog({
      target,
      expected,
      title: isPoolPurge ? "Xoá toàn bộ Trash?" : "Xoá vĩnh viễn?",
      description: isPoolPurge ? "Thao tác này sẽ xoá toàn bộ Trash trong pool. Dữ liệu không thể khôi phục." : "Thao tác này bỏ qua TTL và watcher protection. Dữ liệu không thể khôi phục.",
      label: isPoolPurge ? "Nhập chính xác OK để tiếp tục" : "Nhập chính xác Volume ID để tiếp tục",
    });
    let closed = false;
    const close = () => {
      if (closed) return;
      closed = true;
      form.dataset.confirmOpen = "false";
      if (modal.dialog.open) modal.dialog.close();
      modal.dialog.remove();
      const originalButton = form.querySelector('button[type="submit"]');
      if (originalButton) originalButton.focus();
    };
    modal.input.addEventListener("input", () => { modal.confirm.disabled = modal.input.value.trim() !== expected; });
    modal.cancel.addEventListener("click", close);
    modal.dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
    modal.confirm.addEventListener("click", () => {
      if (modal.input.value.trim() !== expected) return;
      form.elements.confirmation.value = "OK";
      const originalButton = form.querySelector('button[type="submit"]');
      if (originalButton) originalButton.disabled = true;
      close();
      HTMLFormElement.prototype.submit.call(form);
    });
    modal.dialog.showModal();
    modal.input.focus();
  }

  function selectedRows() {
    if (!table) return [];
    return Array.from(table.querySelectorAll("tbody tr")).filter((row) => row.querySelector(".trash-row-select")?.checked);
  }

  function updateBatchState() {
    const checkboxes = table ? Array.from(table.querySelectorAll(".trash-row-select")) : [];
    const selected = checkboxes.filter((checkbox) => checkbox.checked).length;
    if (restoreSelected) restoreSelected.disabled = selected === 0;
    if (deleteSelected) deleteSelected.disabled = selected === 0;
    if (selectAll) {
      selectAll.checked = checkboxes.length > 0 && selected === checkboxes.length;
      selectAll.indeterminate = selected > 0 && selected < checkboxes.length;
    }
  }

  async function restoreBatch(rows) {
    for (const row of rows) {
      const form = row.querySelector(".trash-restore-form");
      const url = new URL(`/api/volumes/${encodeURIComponent(form.dataset.pool)}/trash/${encodeURIComponent(form.dataset.trashId)}/restore`, window.location.origin);
      const cluster = new URLSearchParams(window.location.search).get("cluster");
      if (cluster) url.searchParams.set("cluster", cluster);
      const response = await fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Idempotency-Key": window.crypto?.randomUUID?.() || `ui-${Date.now()}-${Math.random().toString(16).slice(2)}` },
        body: JSON.stringify({ image: form.dataset.image || form.elements.image.value.trim() }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
    }
    window.location.reload();
  }

  async function deleteBatch(rows) {
    for (const row of rows) {
      const form = row.querySelector(".trash-force-remove-form");
      const body = new URLSearchParams({ confirmation: "OK" });
      const response = await fetch(form.action, { method: "POST", credentials: "same-origin", body });
      if (!response.ok) throw new Error(`Không xoá được ${row.dataset.trashId} (HTTP ${response.status})`);
    }
    window.location.reload();
  }

  function openBatchDeleteConfirmation(rows) {
    const ids = rows.map((row) => row.dataset.trashId);
    const dialog = makeDialog({
      target: `${ids.length} Volume đã chọn`,
      expected: "OK",
      title: "Xoá các Volume đã chọn?",
      description: "Thao tác sẽ xoá cưỡng bức các ID bên dưới, bỏ qua TTL và watcher protection. Dữ liệu không thể khôi phục.",
      label: "Nhập chính xác OK để tiếp tục",
    });
    const list = document.createElement("p");
    list.className = "trash-confirm-id-list";
    list.textContent = ids.join(", ");
    dialog.dialog.querySelector(".trash-confirm-target").after(list);
    let closed = false;
    const close = () => { if (closed) return; closed = true; if (dialog.dialog.open) dialog.dialog.close(); dialog.dialog.remove(); };
    dialog.input.addEventListener("input", () => { dialog.confirm.disabled = dialog.input.value.trim() !== "OK"; });
    dialog.cancel.addEventListener("click", close);
    dialog.dialog.addEventListener("cancel", (event) => { event.preventDefault(); close(); });
    dialog.confirm.addEventListener("click", async () => {
      if (dialog.input.value.trim() !== "OK") return;
      dialog.confirm.disabled = true;
      try { await deleteBatch(rows); } catch (error) { close(); window.alert(error.message); }
    });
    dialog.dialog.showModal();
    dialog.input.focus();
  }

  document.querySelectorAll(".trash-copy-id").forEach((button) => button.addEventListener("click", () => copyId(button)));
  document.querySelectorAll(".trash-force-purge-form, .trash-force-remove-form").forEach((form) => form.addEventListener("submit", (event) => { event.preventDefault(); openTrashConfirmation(form); }));

  if (!table || !filterInput || !resetButton || !result || !empty || !pagination || !previousButton || !nextButton || !pageStatus) return;

  const rows = Array.from(table.querySelectorAll("tbody tr"));
  const pageSize = 10;
  let currentPage = 1;
  const normalize = (value) => String(value || "").trim().toLocaleLowerCase("vi");

  document.querySelectorAll(".trash-row-select").forEach((checkbox) => checkbox.addEventListener("change", updateBatchState));
  selectAll?.addEventListener("change", () => { rows.forEach((row) => { row.querySelector(".trash-row-select").checked = selectAll.checked; }); updateBatchState(); });
  restoreSelected?.addEventListener("click", async () => {
    const selected = selectedRows();
    if (!selected.length) return;
    restoreSelected.disabled = true;
    try { await restoreBatch(selected); } catch (error) { window.alert(error.message); updateBatchState(); }
  });
  deleteSelected?.addEventListener("click", () => {
    const selected = selectedRows();
    if (selected.length) openBatchDeleteConfirmation(selected);
  });

  document.querySelectorAll(".trash-restore-form").forEach((form) => form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    try { await restoreBatch([form.closest("tr")]); } catch (error) { window.alert(error.message); button.disabled = false; }
  }));

  function render() {
    const query = normalize(filterInput.value);
    const matches = rows.filter((row) => !query || normalize(row.dataset.trashId).includes(query) || normalize(row.querySelector(".trash-volume-name")?.textContent).includes(query));
    const pageCount = Math.max(1, Math.ceil(matches.length / pageSize));
    currentPage = Math.min(Math.max(1, currentPage), pageCount);
    const start = (currentPage - 1) * pageSize;
    const pageRows = new Set(matches.slice(start, start + pageSize));
    rows.forEach((row) => { row.hidden = !pageRows.has(row); });
    result.textContent = `Hiển thị ${matches.length ? start + 1 : 0}-${Math.min(start + pageSize, matches.length)} / ${matches.length}`;
    pageStatus.textContent = `Trang ${currentPage} / ${pageCount}`;
    previousButton.disabled = currentPage === 1;
    nextButton.disabled = currentPage === pageCount;
    resetButton.disabled = !query;
    empty.hidden = matches.length !== 0;
    table.hidden = matches.length === 0;
    pagination.hidden = matches.length === 0;
    updateBatchState();
  }

  filterInput.addEventListener("input", () => { currentPage = 1; render(); });
  resetButton.addEventListener("click", () => { filterInput.value = ""; currentPage = 1; render(); filterInput.focus(); });
  previousButton.addEventListener("click", () => { if (currentPage > 1) currentPage -= 1; render(); });
  nextButton.addEventListener("click", () => { currentPage += 1; render(); });
  render();
})();
