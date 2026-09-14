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

  function openTrashConfirmation(form) {
    if (form.dataset.confirmOpen === "true") return;
    form.dataset.confirmOpen = "true";

    const target = form.dataset.confirmTarget || "Trash";
    const dialog = document.createElement("dialog");
    dialog.className = "trash-confirm-dialog";
    dialog.setAttribute("aria-labelledby", "trash-confirm-title");
    dialog.innerHTML = `
      <div class="trash-confirm-bar">
        <div class="trash-confirm-icon" aria-hidden="true">!</div>
        <div class="trash-confirm-content">
          <h2 id="trash-confirm-title">Xoá vĩnh viễn?</h2>
          <p>Thao tác với <strong class="trash-confirm-target"></strong> sẽ bỏ qua TTL và watcher protection. Dữ liệu không thể khôi phục.</p>
          <label class="trash-confirm-input-label" for="trash-confirm-input">Nhập chính xác <code>OK</code> để tiếp tục</label>
          <input id="trash-confirm-input" class="trash-confirm-input" type="text" autocomplete="off" spellcheck="false">
          <div class="trash-confirm-actions">
            <button type="button" class="btn btn-ghost btn-sm" data-trash-confirm-cancel>Huỷ</button>
            <button type="button" class="btn btn-danger btn-sm" data-trash-confirm-submit disabled>Xoá vĩnh viễn</button>
          </div>
        </div>
      </div>`;
    dialog.querySelector(".trash-confirm-target").textContent = target;
    document.body.appendChild(dialog);

    const input = dialog.querySelector(".trash-confirm-input");
    const cancelButton = dialog.querySelector("[data-trash-confirm-cancel]");
    const confirmButton = dialog.querySelector("[data-trash-confirm-submit]");
    const originalButton = form.querySelector('button[type="submit"]');
    let closed = false;

    const close = () => {
      if (closed) return;
      closed = true;
      form.dataset.confirmOpen = "false";
      if (dialog.open) dialog.close();
      dialog.remove();
      if (originalButton) originalButton.focus();
    };

    input.addEventListener("input", () => {
      confirmButton.disabled = input.value !== "OK";
    });
    cancelButton.addEventListener("click", close);
    dialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      close();
    });
    confirmButton.addEventListener("click", () => {
      if (input.value !== "OK") return;
      form.elements.confirmation.value = input.value;
      if (originalButton) originalButton.disabled = true;
      close();
      HTMLFormElement.prototype.submit.call(form);
    });

    dialog.showModal();
    input.focus();
  }

  document.querySelectorAll(".trash-force-purge-form, .trash-force-remove-form").forEach((form) => {
    form.addEventListener("submit", (event) => {
      event.preventDefault();
      openTrashConfirmation(form);
    });
  });

  if (!table || !filterInput || !resetButton || !result || !empty || !pagination
      || !previousButton || !nextButton || !pageStatus) return;

  const rows = Array.from(table.querySelectorAll("tbody tr"));
  const pageSize = 10;
  let currentPage = 1;
  const normalize = (value) => String(value || "").trim().toLocaleLowerCase("vi");

  document.querySelectorAll(".trash-restore-form").forEach((form) => {
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      button.disabled = true;
      try {
        const url = new URL(`/api/volumes/${encodeURIComponent(form.dataset.pool)}/trash/${encodeURIComponent(form.dataset.trashId)}/restore`, window.location.origin);
        const cluster = new URLSearchParams(window.location.search).get("cluster");
        if (cluster) url.searchParams.set("cluster", cluster);
        const response = await fetch(url, {
          method: "POST", credentials: "same-origin",
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": (window.crypto && window.crypto.randomUUID)
              ? window.crypto.randomUUID()
              : `ui-${Date.now()}-${Math.random().toString(16).slice(2)}`
          },
          body: JSON.stringify({ image: form.elements.image.value.trim() })
        });
        const body = await response.json().catch(() => ({}));
        if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
        form.replaceChildren(document.createTextNode(`Đã tạo đề xuất ${body.action_id}`));
      } catch (error) {
        window.alert(error.message);
        button.disabled = false;
      }
    });
  });

  function render() {
    const query = normalize(filterInput.value);
    const matches = rows.filter((row) => !query || normalize(row.dataset.trashId).includes(query));
    const pageCount = Math.max(1, Math.ceil(matches.length / pageSize));
    currentPage = Math.min(Math.max(1, currentPage), pageCount);
    const start = (currentPage - 1) * pageSize;
    const pageRows = new Set(matches.slice(start, start + pageSize));

    rows.forEach((row) => { row.hidden = !pageRows.has(row); });
    const shownFrom = matches.length ? start + 1 : 0;
    const shownTo = Math.min(start + pageSize, matches.length);
    result.textContent = `Hiển thị ${shownFrom}-${shownTo} / ${matches.length} Trash`;
    pageStatus.textContent = `Trang ${currentPage} / ${pageCount}`;
    previousButton.disabled = currentPage === 1;
    nextButton.disabled = currentPage === pageCount;
    resetButton.disabled = !query;
    empty.hidden = matches.length !== 0;
    table.hidden = matches.length === 0;
    pagination.hidden = matches.length === 0;
  }

  filterInput.addEventListener("input", () => {
    currentPage = 1;
    render();
  });
  resetButton.addEventListener("click", () => {
    filterInput.value = "";
    currentPage = 1;
    render();
    filterInput.focus();
  });
  previousButton.addEventListener("click", () => {
    if (currentPage > 1) currentPage -= 1;
    render();
  });
  nextButton.addEventListener("click", () => {
    currentPage += 1;
    render();
  });

  render();
})();
