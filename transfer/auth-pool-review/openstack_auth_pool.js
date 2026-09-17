(function () {
  var dataNode = document.getElementById("auth-pool-users-data");
  if (!dataNode) return;

  var searchInput = document.getElementById("auth-pool-search");
  var typeFilter = document.getElementById("auth-pool-type-filter");
  var total = document.getElementById("auth-pool-total");
  var result = document.getElementById("auth-pool-result");
  var tableBody = document.getElementById("auth-pool-body");
  var empty = document.getElementById("auth-pool-empty");
  var pagination = document.getElementById("auth-pool-pagination");
  var paginationSummary = document.getElementById("auth-pool-pagination-summary");
  var pageSizeSelect = document.getElementById("auth-pool-page-size");
  var previousButton = document.getElementById("auth-pool-prev");
  var nextButton = document.getElementById("auth-pool-next");
  var pageStatus = document.getElementById("auth-pool-page-status");
  var detailPanel = document.getElementById("auth-user-detail-panel");
  var detailBackdrop = document.getElementById("auth-user-detail-backdrop");
  var detailClose = document.getElementById("auth-user-detail-close");
  var detailTitle = document.getElementById("auth-user-detail-title");
  var detailContent = document.getElementById("auth-user-detail-content");
  if (!searchInput || !typeFilter || !tableBody || !empty || !pagination || !pageSizeSelect ||
      !previousButton || !nextButton || !pageStatus || !detailPanel || !detailBackdrop ||
      !detailClose || !detailTitle || !detailContent) return;

  var users = [];
  var currentPage = 1;
  var pageSize = 15;
  var detailOpenTimer = null;
  try { users = JSON.parse(dataNode.textContent || "[]"); } catch (parseError) { users = []; }
  if (!Array.isArray(users)) users = [];

  function text(value) { return value == null ? "" : String(value); }
  function capsOf(user, name) {
    var caps = user && user.caps && user.caps[name];
    if (Array.isArray(caps)) return caps.join(", ");
    if (caps && typeof caps === "object") return JSON.stringify(caps);
    return text(caps);
  }
  function classify(user) {
    var entity = text(user && user.entity).toLowerCase();
    var mon = capsOf(user, "mon").toLowerCase();
    var osd = capsOf(user, "osd").toLowerCase();
    var allCaps = mon + " " + osd;
    if (entity === "client.admin" || allCaps.indexOf("allow *") !== -1) return "admin";
    if (entity.indexOf("client.bootstrap-") === 0 || allCaps.indexOf("profile bootstrap") !== -1) return "bootstrap";
    if (/(ceph-exporter|crash|csi-|healthchecker|prometheus|node-exporter|rook|\.mgr$)/.test(entity)) return "service";
    return "application";
  }
  function typeLabel(type) { return {admin: "Admin", bootstrap: "Bootstrap", service: "Service", application: "Application"}[type] || "Application"; }
  function entityNode(entity) {
    var wrapper = document.createElement("span");
    wrapper.className = "auth-user-entity";
    wrapper.title = entity;
    var prefix = entity.indexOf("client.") === 0 ? "client." : "";
    if (prefix) {
      var muted = document.createElement("span"); muted.className = "auth-user-prefix"; muted.textContent = prefix; wrapper.appendChild(muted);
    }
    var name = document.createElement("strong"); name.textContent = prefix ? entity.slice(prefix.length) : entity; wrapper.appendChild(name);
    return wrapper;
  }
  function capParts(value) {
    var cap = text(value).trim();
    if (!cap) return [];
    var parts = cap.split(/,\s*(?=(?:allow|profile)\b)/i).filter(function (part) { return part.trim(); });
    return parts.length ? parts : [cap];
  }
  function capsNode(value, expanded) {
    var parts = capParts(value);
    var holder = document.createElement("div"); holder.className = "auth-cap-holder";
    if (!parts.length) { holder.textContent = "—"; holder.className += " auth-cap-empty"; return holder; }
    var list = document.createElement("div"); list.className = "auth-cap-list" + (expanded ? " is-expanded" : "");
    parts.forEach(function (part) { var chip = document.createElement("span"); chip.className = "auth-cap-chip"; chip.title = part.trim(); chip.textContent = part.trim(); list.appendChild(chip); });
    holder.appendChild(list);
    if (!expanded && parts.length > 4) {
      var more = document.createElement("button"); more.type = "button"; more.className = "auth-cap-more"; more.textContent = "Xem thêm";
      more.addEventListener("click", function (event) { event.stopPropagation(); list.classList.add("is-expanded"); more.hidden = true; }); holder.appendChild(more);
    }
    return holder;
  }
  function typeBadge(type) { var badge = document.createElement("span"); badge.className = "auth-user-type auth-user-type-" + type; badge.textContent = typeLabel(type); return badge; }
  function closeMenus() { Array.prototype.forEach.call(document.querySelectorAll(".auth-user-menu.is-open"), function (menu) { menu.classList.remove("is-open"); }); }
  function field(label, value) { var row = document.createElement("div"); row.className = "auth-detail-field"; var name = document.createElement("dt"); name.textContent = label; var content = document.createElement("dd"); content.textContent = value || "—"; row.appendChild(name); row.appendChild(content); return row; }
  function openDetail(user, revokeRequested) {
    closeMenus();
    var entity = text(user.entity), type = classify(user);
    detailTitle.textContent = entity;
    detailContent.replaceChildren();
    var identity = document.createElement("div"); identity.className = "auth-detail-identity"; identity.appendChild(entityNode(entity)); identity.appendChild(typeBadge(type)); detailContent.appendChild(identity);
    if (revokeRequested) { var warning = document.createElement("p"); warning.className = "auth-detail-warning"; warning.textContent = "Thu hồi quyền là thao tác thay đổi trạng thái Ceph và chưa được thực hiện. Hãy xác nhận qua quy trình quản trị trước khi bổ sung endpoint destructive."; detailContent.appendChild(warning); }
    var definition = document.createElement("dl"); definition.className = "auth-detail-list"; definition.appendChild(field("Entity", entity)); definition.appendChild(field("Loại user", typeLabel(type))); definition.appendChild(field("MON CAPS", capsOf(user, "mon"))); definition.appendChild(field("OSD CAPS", capsOf(user, "osd"))); detailContent.appendChild(definition);
    var monTitle = document.createElement("h3"); monTitle.textContent = "MON capabilities"; detailContent.appendChild(monTitle); detailContent.appendChild(capsNode(capsOf(user, "mon"), true));
    var osdTitle = document.createElement("h3"); osdTitle.textContent = "OSD capabilities"; detailContent.appendChild(osdTitle); detailContent.appendChild(capsNode(capsOf(user, "osd"), true));
    detailPanel.hidden = false; detailBackdrop.hidden = false; if (detailOpenTimer) window.clearTimeout(detailOpenTimer); detailOpenTimer = window.setTimeout(function () { detailPanel.classList.add("is-open"); detailBackdrop.classList.add("is-open"); }, 0); detailClose.focus();
  }
  function closeDetail() { if (detailOpenTimer) { window.clearTimeout(detailOpenTimer); detailOpenTimer = null; } detailPanel.classList.remove("is-open"); detailBackdrop.classList.remove("is-open"); window.setTimeout(function () { detailPanel.hidden = true; detailBackdrop.hidden = true; }, 180); }
  function actionMenu(user) {
    var wrap = document.createElement("div"); wrap.className = "auth-user-actions";
    var button = document.createElement("button"); button.type = "button"; button.className = "auth-user-menu-trigger"; button.setAttribute("aria-label", "Thao tác với " + text(user.entity)); button.textContent = "⋮";
    var menu = document.createElement("div"); menu.className = "auth-user-menu";
    [["Xem chi tiết", function () { openDetail(user, false); }], ["Chỉnh sửa caps", function () { var select = document.getElementById("auth-pool-entity"); if (select) { select.value = text(user.entity); document.querySelector(".auth-pool-grant-card").scrollIntoView({behavior: "smooth", block: "center"}); select.focus(); } }], ["Thu hồi quyền", function () { openDetail(user, true); }]].forEach(function (item) { var option = document.createElement("button"); option.type = "button"; option.className = "auth-user-menu-item"; option.textContent = item[0]; option.addEventListener("click", function (event) { event.stopPropagation(); item[1](); }); menu.appendChild(option); });
    button.addEventListener("click", function (event) { event.stopPropagation(); var wasOpen = menu.classList.contains("is-open"); closeMenus(); if (!wasOpen) menu.classList.add("is-open"); }); wrap.appendChild(button); wrap.appendChild(menu); return wrap;
  }
  function filteredUsers() {
    var query = searchInput.value.trim().toLowerCase(), selectedType = typeFilter.value;
    return users.filter(function (user) { var type = classify(user); var haystack = text(user.entity) + " " + capsOf(user, "mon") + " " + capsOf(user, "osd"); return (!query || haystack.toLowerCase().indexOf(query) !== -1) && (selectedType === "all" || selectedType === type); });
  }
  function render() {
    var visible = filteredUsers(), pages = Math.max(1, Math.ceil(visible.length / pageSize)); currentPage = Math.min(currentPage, pages); var first = (currentPage - 1) * pageSize;
    tableBody.replaceChildren(); empty.hidden = visible.length !== 0; total.textContent = users.length + " users"; result.textContent = visible.length + " phù hợp";
    visible.slice(first, first + pageSize).forEach(function (user) {
      var tr = document.createElement("tr"); tr.className = "auth-user-row"; tr.tabIndex = 0; tr.addEventListener("click", function () { openDetail(user, false); }); tr.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openDetail(user, false); } });
      var identity = document.createElement("td"); identity.className = "auth-user-identity-cell"; identity.appendChild(entityNode(text(user.entity))); identity.appendChild(typeBadge(classify(user))); tr.appendChild(identity);
      var mon = document.createElement("td"); mon.appendChild(capsNode(capsOf(user, "mon"), false)); tr.appendChild(mon);
      var osd = document.createElement("td"); osd.appendChild(capsNode(capsOf(user, "osd"), false)); tr.appendChild(osd);
      var actions = document.createElement("td"); actions.className = "auth-user-actions-cell"; actions.appendChild(actionMenu(user)); tr.appendChild(actions); tableBody.appendChild(tr);
    });
    pagination.hidden = visible.length <= pageSize; paginationSummary.textContent = visible.length ? "Hiển thị " + (first + 1) + "-" + Math.min(first + pageSize, visible.length) + " / " + visible.length + " users" : "0 users";
    pageStatus.textContent = "Trang " + currentPage + " / " + pages; previousButton.disabled = currentPage <= 1; nextButton.disabled = currentPage >= pages;
  }
  searchInput.addEventListener("input", function () { currentPage = 1; render(); }); typeFilter.addEventListener("change", function () { currentPage = 1; render(); }); pageSizeSelect.addEventListener("change", function () { pageSize = Number(pageSizeSelect.value) || 15; currentPage = 1; render(); }); previousButton.addEventListener("click", function () { if (currentPage > 1) { currentPage -= 1; render(); } }); nextButton.addEventListener("click", function () { if (currentPage < Math.ceil(filteredUsers().length / pageSize)) { currentPage += 1; render(); } });
  detailClose.addEventListener("click", closeDetail); detailBackdrop.addEventListener("click", closeDetail); document.addEventListener("click", closeMenus); document.addEventListener("keydown", function (event) { if (event.key === "Escape") { closeMenus(); if (!detailPanel.hidden) closeDetail(); } }); render();
})();

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
