(function () {
  var panel = document.getElementById("ceph-config-dump-panel");
  var loadButton = document.getElementById("ceph-config-dump-load");
  var filterInput = document.getElementById("ceph-config-dump-filter-input");
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
  var pagination = document.getElementById("ceph-config-dump-pagination");
  var previousButton = document.getElementById("ceph-config-dump-prev");
  var nextButton = document.getElementById("ceph-config-dump-next");
  var pageStatus = document.getElementById("ceph-config-dump-page-status");
  if (!panel || !loadButton || !filterInput || !tableWrap || !tableBody || !status || !error ||
      !form || !actionInput || !sectionInput || !nameInput || !valueInput || !submitButton || !resetButton ||
      !pagination || !previousButton || !nextButton || !pageStatus) return;

  var rows = [];
  var currentPage = 1;
  var pageSize = 10;

  function render() {
    var query = filterInput.value.trim().toLowerCase();
    tableBody.replaceChildren();
    var visible = rows.filter(function (row) {
      return !query || (row.section + " " + row.name + " " + row.value).toLowerCase().indexOf(query) !== -1;
    });
    var pageCount = Math.max(1, Math.ceil(visible.length / pageSize));
    currentPage = Math.min(currentPage, pageCount);
    var first = (currentPage - 1) * pageSize;
    var pageRows = visible.slice(first, first + pageSize);
    if (!visible.length) {
      var empty = document.createElement("tr");
      empty.className = "empty-row";
      var cell = document.createElement("td");
      cell.colSpan = 6;
      cell.textContent = query ? "Không có option phù hợp." : "Cụm không trả về option nào.";
      empty.appendChild(cell);
      tableBody.appendChild(empty);
    } else {
      pageRows.forEach(function (row) {
        var tr = document.createElement("tr");
        [row.section, row.name, row.value, row.level, row.can_update_at_runtime ? "Có" : "Không"].forEach(function (value) {
          var td = document.createElement("td");
          td.textContent = value == null ? "" : String(value);
          tr.appendChild(td);
        });
        var actions = document.createElement("td");
        var edit = document.createElement("button");
        edit.type = "button";
        edit.className = "btn";
        edit.textContent = "Sửa";
        edit.addEventListener("click", function () {
          actionInput.value = "set";
          sectionInput.value = row.section;
          nameInput.value = row.name;
          valueInput.value = row.redacted ? "" : (row.value || "");
          valueInput.required = Boolean(row.redacted);
          submitButton.textContent = "Cập nhật";
          resetButton.hidden = false;
          valueInput.focus();
          status.textContent = row.redacted
            ? "Option nhạy cảm đã được che; nhập giá trị mới rồi bấm Cập nhật."
            : "Đang sửa " + row.section + "." + row.name + ".";
        });
        actions.appendChild(edit);
        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "btn";
        remove.textContent = "Xóa";
        remove.addEventListener("click", function () {
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
        });
        actions.appendChild(document.createTextNode(" "));
        actions.appendChild(remove);
        tr.appendChild(actions);
        tableBody.appendChild(tr);
      });
    }
    tableWrap.hidden = false;
    pagination.hidden = visible.length <= pageSize;
    previousButton.disabled = currentPage <= 1;
    nextButton.disabled = currentPage >= pageCount;
    pageStatus.textContent = "Trang " + currentPage + "/" + pageCount;
    var last = Math.min(first + pageRows.length, visible.length);
    status.textContent = visible.length
      ? "Hiển thị " + (first + 1) + "–" + last + "/" + visible.length + " option (tổng " + rows.length + ")."
      : "Không có option phù hợp.";
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
        status.textContent = "Cụm " + ((body.cluster && body.cluster.name) || "đang chọn") + ": " + rows.length + " option.";
      })
      .catch(function (reason) {
        tableWrap.hidden = true;
        error.textContent = reason.message || "Không tải được cấu hình Ceph";
        error.hidden = false;
        status.textContent = "Chưa tải dữ liệu.";
      })
      .finally(function () { loadButton.disabled = false; });
  }

  loadButton.addEventListener("click", loadConfig);

  previousButton.addEventListener("click", function () {
    if (currentPage > 1) { currentPage -= 1; render(); }
  });
  nextButton.addEventListener("click", function () {
    var pageCount = Math.max(1, Math.ceil(rows.length / pageSize));
    if (currentPage < pageCount) { currentPage += 1; render(); }
  });

  filterInput.addEventListener("input", function () {
    currentPage = 1;
    if (rows.length) render();
  });

  resetButton.addEventListener("click", function () {
    form.reset();
    actionInput.value = "set";
    valueInput.required = false;
    submitButton.textContent = "Tạo / Cập nhật";
    resetButton.hidden = true;
    status.textContent = rows.length ? "Đã hủy chỉnh sửa." : "Chưa tải dữ liệu.";
  });

  form.addEventListener("submit", function (event) {
    if (!window.confirm("Lưu thay đổi và restart toàn bộ RGW của cụm này?")) {
      event.preventDefault();
    }
  });

  // Load immediately so operators see the first page without an extra click.
  loadConfig();
})();
