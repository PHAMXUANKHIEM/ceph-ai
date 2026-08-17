(function () {
  var main = document.querySelector("main.page");
  if (!main) return;
  var panels = Array.prototype.slice.call(main.querySelectorAll(":scope > section.card"));
  if (!panels.length) return;
  var labels = ["Danh sách S3 user", "Quản lý S3 user", "Access-key lifecycle", "Object Storage Audit"];
  var tabs = document.createElement("nav");
  tabs.className = "bucket-feature-tabs";
  tabs.setAttribute("role", "tablist");
  tabs.setAttribute("aria-label", "Tính năng S3 Users");

  function activate(selected) {
    Array.prototype.forEach.call(tabs.querySelectorAll("[data-s3-user-tab]"), function (tab) {
      var active = tab === selected;
      tab.classList.toggle("is-active", active);
      tab.setAttribute("aria-selected", String(active));
      tab.tabIndex = active ? 0 : -1;
    });
    panels.forEach(function (panel) { panel.hidden = panel.id !== selected.dataset.s3UserTab; });
  }

  panels.forEach(function (panel, index) {
    var id = "s3-user-feature-" + index;
    var button = document.createElement("button");
    panel.id = id;
    panel.classList.add("bucket-feature-panel");
    panel.setAttribute("role", "tabpanel");
    button.type = "button";
    button.className = "bucket-feature-tab";
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", id);
    button.dataset.s3UserTab = id;
    button.textContent = labels[index] || panel.querySelector("h2").textContent;
    button.addEventListener("click", function () { activate(button); });
    button.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      var offset = event.key === "ArrowRight" ? 1 : -1;
      var nextIndex = (index + offset + panels.length) % panels.length;
      var next = tabs.querySelectorAll("[data-s3-user-tab]")[nextIndex];
      activate(next);
      next.focus();
    });
    tabs.appendChild(button);
  });
  main.insertBefore(tabs, panels[0]);
  activate(tabs.querySelector("[data-s3-user-tab]"));
})();

(function () {
  var form = document.getElementById("s3-user-action-form");
  if (!form) return;
  var action = document.getElementById("s3-action");
  var uid = document.getElementById("s3-uid");
  var displayName = document.getElementById("s3-display-name");
  var email = document.getElementById("s3-email");
  var preview = document.getElementById("s3-preview");
  var summary = document.getElementById("s3-preview-summary");
  var command = document.getElementById("s3-preview-command");
  var confirmation = document.getElementById("s3-confirmation");
  var execute = document.getElementById("s3-execute");
  var status = document.getElementById("s3-action-status");
  var approvedPayload = null;
  var managementHint = form.closest(".card").querySelector(".card-header .hint");
  if (managementHint) managementHint.textContent = "Mọi thao tác yêu cầu preview và nhập lại UID. Access key được quản lý riêng ở phần Access-key lifecycle.";
  function payload() { return {action: action.value, uid: uid.value.trim(), display_name: displayName.value.trim(), email: email.value.trim()}; }
  function endpoint(kind) { return "/api/object-storage/users/actions/" + kind + "?cluster=" + encodeURIComponent(form.dataset.cluster); }
  function errorText(response) { return response.json().then(function (body) { throw new Error(body.detail || "Thao tác thất bại"); }); }
  form.addEventListener("submit", function (event) {
    event.preventDefault(); approvedPayload = null; execute.disabled = true; preview.hidden = true; status.textContent = "Đang tạo preview...";
    fetch(endpoint("preview"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload())})
      .then(function (response) { return response.ok ? response.json() : errorText(response); })
      .then(function (data) { approvedPayload = payload(); summary.textContent = data.action + " S3 user " + data.uid + " trên cluster " + data.cluster_name + " · rủi ro " + data.risk; command.textContent = data.preview; confirmation.value = ""; preview.hidden = false; status.textContent = "Kiểm tra preview rồi nhập lại UID."; })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; });
  });
  confirmation.addEventListener("input", function () { execute.disabled = !approvedPayload || confirmation.value !== approvedPayload.uid; });
  execute.addEventListener("click", function () {
    if (!approvedPayload) return; execute.disabled = true; status.textContent = "Đang thực thi...";
    var body = Object.assign({}, approvedPayload, {confirmation: confirmation.value});
    fetch(endpoint("execute"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(function (response) { return response.ok ? response.json() : errorText(response); })
      .then(function (data) { if (data.credential) { document.getElementById("s3-created-access-key").textContent = data.credential.access_key; document.getElementById("s3-created-secret-key").textContent = data.credential.secret_key; document.getElementById("s3-one-time-secret").hidden = false; status.textContent = "Đã tạo user. Hãy lưu credential trước khi rời trang."; } else { status.textContent = "Thao tác thành công. Đang tải lại..."; window.location.reload(); } })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; execute.disabled = false; });
  });
})();

(function () {
  var form = document.getElementById("s3-key-action-form");
  if (!form) return;
  var action = document.getElementById("s3-key-action");
  var uid = document.getElementById("s3-key-uid");
  var accessKey = document.getElementById("s3-access-key");
  var preview = document.getElementById("s3-key-preview");
  var summary = document.getElementById("s3-key-preview-summary");
  var confirmation = document.getElementById("s3-key-confirmation");
  var execute = document.getElementById("s3-key-execute");
  var status = document.getElementById("s3-key-status");
  var secretBox = document.getElementById("s3-one-time-secret");
  var approved = null;
  function body() { return {action: action.value, uid: uid.value.trim(), access_key: accessKey.value.trim()}; }
  function endpoint(kind) { return "/api/object-storage/users/keys/" + kind + "?cluster=" + encodeURIComponent(form.dataset.cluster); }
  function parse(response) { return response.ok ? response.json() : response.json().then(function (data) { throw new Error(data.detail || "Thao tác thất bại"); }); }
  form.addEventListener("submit", function (event) {
    event.preventDefault(); approved = null; preview.hidden = true; secretBox.hidden = true; execute.disabled = true; status.textContent = "Đang tạo preview...";
    fetch(endpoint("preview"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body())})
      .then(parse).then(function (data) { approved = body(); approved.expected = data.confirmation_required; summary.textContent = data.preview + " · " + data.cluster_name + " · rủi ro " + data.risk; confirmation.value = ""; preview.hidden = false; status.textContent = "Kiểm tra preview và nhập giá trị xác nhận."; })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; });
  });
  confirmation.addEventListener("input", function () { execute.disabled = !approved || confirmation.value !== approved.expected; });
  execute.addEventListener("click", function () {
    if (!approved) return; execute.disabled = true; var payload = {action: approved.action, uid: approved.uid, access_key: approved.access_key, confirmation: confirmation.value}; status.textContent = "Đang thực thi...";
    fetch(endpoint("execute"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload)})
      .then(parse).then(function (data) { if (data.credential) { document.getElementById("s3-created-access-key").textContent = data.credential.access_key; document.getElementById("s3-created-secret-key").textContent = data.credential.secret_key; secretBox.hidden = false; } status.textContent = "Thao tác thành công. Request ID: " + data.request_id; approved = null; preview.hidden = true; })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; execute.disabled = false; });
  });
})();

(function () {
  var headings = Array.prototype.slice.call(document.querySelectorAll("main.page section.card h2"));
  var heading = headings.find(function (item) { return item.textContent.trim() === "Object Storage Audit"; });
  if (!heading) return;
  var panel = heading.closest("section.card");
  var table = panel.querySelector("table");
  var tbody = table && table.querySelector("tbody");
  if (!tbody) return;
  var rows = Array.prototype.slice.call(tbody.querySelectorAll("tr:not(.empty-row)"));
  var page = 1;
  var pageSize = 10;
  var noResults = document.createElement("tr");
  noResults.className = "empty-row object-storage-audit-no-results";
  noResults.innerHTML = '<td colspan="6"><div class="empty-state">Không có bản ghi audit phù hợp.</div></td>';
  noResults.hidden = true;
  tbody.appendChild(noResults);

  var controls = document.createElement("div");
  controls.className = "object-storage-audit-controls";
  controls.innerHTML = '<label>Tìm kiếm<input type="search" maxlength="120" placeholder="Actor, action, target hoặc request ID"></label>' +
    '<label>Action<select><option value="">Tất cả action</option></select></label>' +
    '<label>Kết quả<select><option value="">Tất cả kết quả</option><option value="succeeded">Succeeded</option><option value="failed">Failed</option><option value="pending">Pending</option></select></label>';
  panel.insertBefore(controls, table.closest(".table-wrap"));
  var search = controls.querySelector("input");
  var actionFilter = controls.querySelectorAll("select")[0];
  var resultFilter = controls.querySelectorAll("select")[1];
  Array.from(new Set(rows.map(function (row) { return row.cells[2].textContent.trim(); }))).sort().forEach(function (action) {
    var option = document.createElement("option");
    option.value = action;
    option.textContent = action;
    actionFilter.appendChild(option);
  });

  var pagination = document.createElement("div");
  pagination.className = "object-storage-audit-pagination";
  pagination.innerHTML = '<button type="button" class="btn btn-ghost btn-sm">← Trang trước</button>' +
    '<span></span><button type="button" class="btn btn-ghost btn-sm">Trang sau →</button>';
  panel.appendChild(pagination);
  var previous = pagination.querySelectorAll("button")[0];
  var next = pagination.querySelectorAll("button")[1];
  var status = pagination.querySelector("span");

  function render() {
    var query = search.value.trim().toLowerCase();
    var filtered = rows.filter(function (row) {
      var action = row.cells[2].textContent.trim();
      var result = row.cells[4].textContent.trim().toLowerCase();
      return (!query || row.textContent.toLowerCase().indexOf(query) !== -1) &&
        (!actionFilter.value || action === actionFilter.value) &&
        (!resultFilter.value || result === resultFilter.value);
    });
    var pages = Math.max(1, Math.ceil(filtered.length / pageSize));
    page = Math.min(page, pages);
    rows.forEach(function (row) { row.hidden = true; });
    filtered.slice((page - 1) * pageSize, page * pageSize).forEach(function (row) { row.hidden = false; });
    noResults.hidden = filtered.length !== 0 || rows.length === 0;
    status.textContent = "Trang " + page + " / " + pages + " · " + filtered.length + " bản ghi · 10 dòng/trang";
    previous.disabled = page === 1;
    next.disabled = page === pages;
  }
  [search, actionFilter, resultFilter].forEach(function (control) {
    control.addEventListener(control === search ? "input" : "change", function () { page = 1; render(); });
  });
  previous.addEventListener("click", function () { page = Math.max(1, page - 1); render(); });
  next.addEventListener("click", function () { page += 1; render(); });
  render();
})();
