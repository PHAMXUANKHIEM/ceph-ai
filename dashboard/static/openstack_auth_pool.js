(function () {
  var panel = document.getElementById("ceph-config-dump-panel");
  var loadButton = document.getElementById("ceph-config-dump-load");
  var filterInput = document.getElementById("ceph-config-dump-filter-input");
  var levelFilter = document.getElementById("ceph-config-dump-level-filter");
  var countBadge = document.getElementById("ceph-config-dump-count");
  var tableWrap = document.getElementById("ceph-config-dump-table-wrap");
  var tableBody = document.querySelector("#ceph-config-dump-table tbody");
  var status = document.getElementById("ceph-config-dump-status");
  var error = document.getElementById("ceph-config-dump-error");
  var form = document.getElementById("ceph-config-dump-form");
  var actionInput = document.getElementById("ceph-config-dump-action");
  var sectionInput = document.getElementById("ceph-config-dump-section");
  var nameInput = document.getElementById("ceph-config-dump-name");
  var valueInput = document.getElementById("ceph-config-dump-value");
  var submitButton = document.getElementById("ceph-config-dump-submit");
  var resetButton = document.getElementById("ceph-config-dump-reset");
  var editorToggle = document.getElementById("ceph-config-editor-toggle");
  var editorContent = document.getElementById("ceph-config-editor-content");
  var pagination = document.getElementById("ceph-config-dump-pagination");
  var previousButton = document.getElementById("ceph-config-dump-prev");
  var nextButton = document.getElementById("ceph-config-dump-next");
  var pageSummary = document.getElementById("ceph-config-dump-summary");
  var pageStatus = document.getElementById("ceph-config-dump-page-status");
  var pageSizeSelect = document.getElementById("ceph-config-dump-page-size");
  var pageButtons = document.getElementById("ceph-config-dump-page-buttons");
  if (!panel || !loadButton || !filterInput || !levelFilter || !countBadge || !tableWrap || !tableBody ||
      !status || !error || !form || !actionInput || !sectionInput || !nameInput || !valueInput ||
      !submitButton || !resetButton || !editorToggle || !editorContent || !pagination || !previousButton || !nextButton || !pageStatus || !pageSummary ||
      !pageSizeSelect || !pageButtons) return;

  var rows = [];
  var currentPage = 1;
  var expandedSections = Object.create(null);

  function text(value) {
    return value == null ? "" : String(value);
  }

  function setEditorOpen(open) {
    editorContent.hidden = !open;
    editorToggle.setAttribute("aria-expanded", open ? "true" : "false");
    editorToggle.querySelector(".config-editor-chevron").textContent = open ? "▼" : "▶";
  }

  function makeLevelBadge(level) {
    var badge = document.createElement("span");
    var normalized = text(level).toLowerCase();
    badge.className = "config-level-badge" + (normalized ? " is-" + normalized : "");
    badge.textContent = text(level) || "—";
    return badge;
  }

  function makeValueCell(row) {
    var cell = document.createElement("td");
    cell.className = "config-value-cell";
    var value = text(row.value);
    cell.title = row.redacted ? "Giá trị nhạy cảm đã được che" : (value || "Không có giá trị");
    if (value.indexOf(",") !== -1 && value.split(",").filter(function (part) { return part.trim(); }).length > 1) {
      var chips = document.createElement("div");
      chips.className = "config-value-chips";
      value.split(",").forEach(function (part) {
        var item = part.trim();
        if (!item) return;
        var chip = document.createElement("span");
        chip.className = "config-value-chip";
        chip.textContent = item;
        chips.appendChild(chip);
      });
      cell.appendChild(chips);
    } else {
      var preview = document.createElement("span");
      preview.className = "config-value-truncate";
      preview.textContent = value || "—";
      cell.appendChild(preview);
    }
    return cell;
  }

  function makeActionButton(icon, label, callback) {
    var button = document.createElement("button");
    button.type = "button";
    button.className = "config-row-action";
    button.setAttribute("aria-label", label);
    button.title = label;
    button.textContent = icon;
    button.addEventListener("click", function (event) {
      event.stopPropagation();
      callback();
    });
    return button;
  }

  function startEdit(row) {
    actionInput.value = "set";
    sectionInput.value = text(row.section);
    nameInput.value = text(row.name);
    valueInput.value = row.redacted ? "" : text(row.value);
    valueInput.required = Boolean(row.redacted);
    submitButton.textContent = "Cập nhật";
    resetButton.hidden = false;
    setEditorOpen(true);
    status.textContent = row.redacted
      ? "Option nhạy cảm đã được che; nhập giá trị mới rồi bấm Cập nhật."
      : "Đang sửa " + row.section + "." + row.name + ".";
    window.setTimeout(function () { valueInput.focus(); }, 0);
  }

  function deleteRow(row) {
    if (!window.confirm("Xóa option " + row.section + "." + row.name + " và restart RGW?")) return;
    var deleteForm = document.createElement("form");
    deleteForm.method = "post";
    deleteForm.action = "/openstack/config-dump?cluster=" + encodeURIComponent(panel.dataset.cluster || "");
    [["action", "rm"], ["section", row.section], ["name", row.name]].forEach(function (entry) {
      var input = document.createElement("input");
      input.type = "hidden";
      input.name = entry[0];
      input.value = entry[1];
      deleteForm.appendChild(input);
    });
    document.body.appendChild(deleteForm);
    deleteForm.submit();
  }

  function pageItems(pageCount) {
    if (pageCount <= 7) {
      return Array.from({ length: pageCount }, function (_unused, index) { return index + 1; });
    }
    if (currentPage <= 4) return [1, 2, 3, 4, 5, "…", pageCount];
    if (currentPage >= pageCount - 3) return [1, "…", pageCount - 4, pageCount - 3, pageCount - 2, pageCount - 1, pageCount];
    return [1, "…", currentPage - 1, currentPage, currentPage + 1, "…", pageCount];
  }

  function appendPageButton(label, page, disabled, active, ariaLabel) {
    if (label === "…") {
      var ellipsis = document.createElement("span");
      ellipsis.className = "config-page-ellipsis";
      ellipsis.textContent = label;
      pageButtons.appendChild(ellipsis);
      return;
    }
    var button = document.createElement("button");
    button.type = "button";
    button.className = "config-page-button" + (active ? " is-active" : "");
    button.textContent = label;
    button.disabled = Boolean(disabled);
    button.setAttribute("aria-label", ariaLabel || ("Trang " + page));
    if (active) button.setAttribute("aria-current", "page");
    button.addEventListener("click", function () {
      currentPage = page;
      render();
    });
    pageButtons.appendChild(button);
  }

  function renderPagination(pageCount) {
    pageButtons.replaceChildren();
    previousButton.disabled = currentPage <= 1;
    nextButton.disabled = currentPage >= pageCount;
    pageItems(pageCount).forEach(function (page) {
      appendPageButton(page, page, false, page === currentPage);
    });
  }

  function render() {
    var query = filterInput.value.trim().toLowerCase();
    var selectedLevel = levelFilter.value.toLowerCase();
    var visible = rows.filter(function (row) {
      var searchable = (text(row.section) + " " + text(row.name)).toLowerCase();
      var levelMatches = !selectedLevel || text(row.level).toLowerCase() === selectedLevel;
      return (!query || searchable.indexOf(query) !== -1) && levelMatches;
    });
    var pageSize = pageSizeSelect.value === "all" ? Math.max(visible.length, 1) : Number(pageSizeSelect.value) || 25;
    var pageCount = Math.max(1, Math.ceil(visible.length / pageSize));
    currentPage = Math.min(currentPage, pageCount);
    var first = (currentPage - 1) * pageSize;
    var pageRows = visible.slice(first, first + pageSize);
    tableBody.replaceChildren();

    if (!visible.length) {
      var empty = document.createElement("tr");
      empty.className = "config-empty-row";
      var emptyCell = document.createElement("td");
      emptyCell.colSpan = 4;
      emptyCell.textContent = query || selectedLevel ? "Không có option phù hợp." : "Cụm không trả về option nào.";
      empty.appendChild(emptyCell);
      tableBody.appendChild(empty);
    } else {
      var groups = [];
      var bySection = Object.create(null);
      pageRows.forEach(function (row) {
        var key = text(row.section) || "unknown";
        if (!bySection[key]) {
          bySection[key] = { section: key, rows: [] };
          groups.push(bySection[key]);
        }
        bySection[key].rows.push(row);
      });
      groups.forEach(function (group) {
        var collapsed = expandedSections[group.section] === false;
        var sectionRow = document.createElement("tr");
        sectionRow.className = "config-section-row" + (collapsed ? " is-collapsed" : "");
        var sectionCell = document.createElement("th");
        sectionCell.colSpan = 4;
        sectionCell.scope = "rowgroup";
        var sectionButton = document.createElement("button");
        sectionButton.type = "button";
        sectionButton.className = "config-section-toggle";
        sectionButton.setAttribute("aria-expanded", collapsed ? "false" : "true");
        sectionButton.innerHTML = '<span class="config-section-chevron" aria-hidden="true">' + (collapsed ? "▶" : "▼") + '</span>';
        var sectionName = document.createElement("span");
        sectionName.className = "config-section-name";
        sectionName.textContent = group.section;
        sectionButton.appendChild(sectionName);
        var sectionCount = document.createElement("span");
        sectionCount.className = "config-section-count";
        sectionCount.textContent = group.rows.length + " options";
        sectionButton.appendChild(sectionCount);
        sectionButton.addEventListener("click", function () {
          expandedSections[group.section] = collapsed;
          render();
        });
        sectionCell.appendChild(sectionButton);
        sectionRow.appendChild(sectionCell);
        tableBody.appendChild(sectionRow);

        if (collapsed) return;
        group.rows.forEach(function (row) {
          var tr = document.createElement("tr");
          tr.className = "config-option-row";
          var option = document.createElement("td");
          option.className = "config-option-cell";
          option.textContent = text(row.name);
          tr.appendChild(option);
          tr.appendChild(makeValueCell(row));
          var level = document.createElement("td");
          level.className = "config-level-cell";
          level.appendChild(makeLevelBadge(row.level));
          tr.appendChild(level);
          var actions = document.createElement("td");
          actions.className = "config-actions-cell";
          actions.appendChild(makeActionButton("✏️", "Sửa " + row.section + "." + row.name, function () { startEdit(row); }));
          actions.appendChild(makeActionButton("🗑️", "Xóa " + row.section + "." + row.name, function () { deleteRow(row); }));
          tr.appendChild(actions);
          tableBody.appendChild(tr);
        });
      });
    }

    tableWrap.hidden = false;
    pagination.hidden = !visible.length;
    countBadge.textContent = rows.length + " options";
    var last = Math.min(first + pageRows.length, visible.length);
    var summary = "Hiển thị " + (visible.length ? first + 1 : 0) + "–" + last + " / " + visible.length + " mục";
    pageSummary.textContent = summary;
    pageSummary.dataset.mobileSummary = (visible.length ? first + 1 : 0) + "–" + last + " / " + visible.length;
    pageStatus.textContent = "Trang " + currentPage + "/" + pageCount;
    previousButton.disabled = currentPage <= 1;
    nextButton.disabled = currentPage >= pageCount;
    renderPagination(pageCount);
    if (visible.length) status.textContent = "Đang hiển thị " + visible.length + " option phù hợp.";
    else status.textContent = query || selectedLevel ? "Không có option phù hợp." : "Cụm không trả về option nào.";
  }

  function loadConfig() {
    loadButton.disabled = true;
    error.hidden = true;
    status.textContent = "Đang tải ceph config dump…";
    var cluster = encodeURIComponent(panel.dataset.cluster || "");
    fetch("/api/openstack/auth-config-dump?cluster=" + cluster, { credentials: "same-origin" })
      .then(function (response) {
        return response.json().then(function (body) {
          if (!response.ok) throw new Error(body.detail || "Không tải được cấu hình Ceph");
          return body;
        });
      })
      .then(function (body) {
        rows = Array.isArray(body.rows) ? body.rows : [];
        currentPage = 1;
        render();
        status.textContent = "Cụm " + ((body.cluster && body.cluster.name) || "đang chọn") + ": " + rows.length + " options.";
      })
      .catch(function (reason) {
        tableWrap.hidden = true;
        pagination.hidden = true;
        countBadge.textContent = "0 options";
        error.textContent = reason.message || "Không tải được cấu hình Ceph";
        error.hidden = false;
        status.textContent = "Chưa tải dữ liệu.";
      })
      .finally(function () { loadButton.disabled = false; });
  }

  editorToggle.addEventListener("click", function () {
    setEditorOpen(editorContent.hidden);
  });
  loadButton.addEventListener("click", loadConfig);
  filterInput.addEventListener("input", function () { currentPage = 1; render(); });
  levelFilter.addEventListener("change", function () { currentPage = 1; render(); });
  pageSizeSelect.addEventListener("change", function () { currentPage = 1; render(); });
  previousButton.addEventListener("click", function () { if (currentPage > 1) { currentPage -= 1; render(); } });
  nextButton.addEventListener("click", function () { currentPage += 1; render(); });

  resetButton.addEventListener("click", function () {
    form.reset();
    actionInput.value = "set";
    valueInput.required = false;
    submitButton.textContent = "Tạo / Cập nhật";
    resetButton.hidden = true;
    setEditorOpen(false);
    status.textContent = rows.length ? "Đã hủy chỉnh sửa." : "Chưa tải dữ liệu.";
  });

  form.addEventListener("submit", function (event) {
    if (!window.confirm("Lưu thay đổi và restart toàn bộ RGW của cụm này?")) event.preventDefault();
  });

  setEditorOpen(false);
  loadConfig();
})();

// Auth-Pool user inventory uses the same pagination contract as every other
// dashboard list. The table is already server-rendered, so filtering stays
// local and does not disturb the capability forms above it.
(function () {
  var table = document.getElementById("auth-user-table");
  if (!table) return;
  var rows = Array.prototype.slice.call(table.querySelectorAll("tbody tr"));
  var previous = document.getElementById("auth-user-prev");
  var next = document.getElementById("auth-user-next");
  var pages = document.getElementById("auth-user-page-buttons");
  var status = document.getElementById("auth-user-page-status");
  var summary = document.getElementById("auth-user-page-summary");
  var sizeSelect = document.getElementById("auth-user-page-size");
  var search = document.getElementById("auth-user-search");
  var kind = document.getElementById("auth-user-type");
  if (!previous || !next || !pages || !status || !summary || !sizeSelect) return;
  var page = 1;
  function render() {
    var query = String(search && search.value || "").trim().toLowerCase();
    var wanted = String(kind && kind.value || "");
    var visible = rows.filter(function (row) {
      return (!query || String(row.dataset.authUser || "").indexOf(query) !== -1) && (!wanted || row.dataset.authKind === wanted);
    });
    var pageSize = Number(sizeSelect.value) || 10;
    var pageCount = Math.max(1, Math.ceil(visible.length / pageSize));
    page = Math.min(page, pageCount);
    var first = (page - 1) * pageSize;
    var last = Math.min(first + pageSize, visible.length);
    rows.forEach(function (row) { row.hidden = visible.indexOf(row) < first || visible.indexOf(row) >= last; });
    var text = "Hiển thị " + (visible.length ? first + 1 : 0) + "–" + last + " / " + visible.length + " mục";
    summary.textContent = text; summary.dataset.mobileSummary = (visible.length ? first + 1 : 0) + "–" + last + " / " + visible.length;
    status.textContent = "Trang " + page + "/" + pageCount;
    previous.disabled = page <= 1; next.disabled = page >= pageCount;
    if (window.DashboardPagination) window.DashboardPagination.renderPages(pages, page, pageCount, function (target) { page = target; render(); });
  }
  previous.addEventListener("click", function () { if (page > 1) { page -= 1; render(); } });
  next.addEventListener("click", function () { page += 1; render(); });
  sizeSelect.addEventListener("change", function () { page = 1; render(); });
  if (search) search.addEventListener("input", function () { page = 1; render(); });
  if (kind) kind.addEventListener("change", function () { page = 1; render(); });
  render();
}());
