(function () {
  var panel = document.getElementById("ceph-config-dump-panel");
  var loadButton = document.getElementById("ceph-config-dump-load");
  var filterInput = document.getElementById("ceph-config-dump-filter-input");
  var tableWrap = document.getElementById("ceph-config-dump-table-wrap");
  var tableBody = document.querySelector("#ceph-config-dump-table tbody");
  var status = document.getElementById("ceph-config-dump-status");
  var error = document.getElementById("ceph-config-dump-error");
  if (!panel || !loadButton || !filterInput || !tableWrap || !tableBody || !status || !error) return;

  var rows = [];

  function render() {
    var query = filterInput.value.trim().toLowerCase();
    tableBody.replaceChildren();
    var visible = rows.filter(function (row) {
      return !query || (row.section + " " + row.name + " " + row.value).toLowerCase().indexOf(query) !== -1;
    });
    if (!visible.length) {
      var empty = document.createElement("tr");
      empty.className = "empty-row";
      var cell = document.createElement("td");
      cell.colSpan = 5;
      cell.textContent = query ? "Không có option phù hợp." : "Cụm không trả về option nào.";
      empty.appendChild(cell);
      tableBody.appendChild(empty);
    } else {
      visible.forEach(function (row) {
        var tr = document.createElement("tr");
        [row.section, row.name, row.value, row.level, row.can_update_at_runtime ? "Có" : "Không"].forEach(function (value) {
          var td = document.createElement("td");
          td.textContent = value == null ? "" : String(value);
          tr.appendChild(td);
        });
        tableBody.appendChild(tr);
      });
    }
    tableWrap.hidden = false;
    status.textContent = "Hiển thị " + visible.length + "/" + rows.length + " option.";
  }

  loadButton.addEventListener("click", function () {
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
  });

  filterInput.addEventListener("input", function () {
    if (rows.length) render();
  });
})();
