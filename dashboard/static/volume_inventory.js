(function () {
  "use strict";

  var panel = document.getElementById("volume-inventory-panel");
  if (!panel) return;

  var pool = panel.dataset.pool;
  var form = document.getElementById("volume-inventory-filter");
  var search = document.getElementById("volume-inventory-search");
  var sort = document.getElementById("volume-inventory-sort");
  var tbody = document.querySelector("#volume-inventory-table tbody");
  var error = document.getElementById("volume-inventory-error");
  var freshness = document.getElementById("volume-inventory-freshness");
  var pager = document.getElementById("volume-inventory-pagination");
  var prev = document.getElementById("volume-inventory-prev");
  var next = document.getElementById("volume-inventory-next");
  var pageStatus = document.getElementById("volume-inventory-page-status");
  var detail = document.getElementById("volume-inventory-detail");
  var state = { page: 1, pages: 1, loading: false };

  function bytes(value) {
    var n = Number(value || 0);
    var units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + units[i];
  }

  function requestJson(url) {
    return fetch(url, { credentials: "same-origin" }).then(function (response) {
      if (response.redirected && response.url.indexOf("/login") !== -1) {
        window.location.reload();
        throw new Error("unauthenticated");
      }
      if (!response.ok) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          throw new Error(body.detail || "HTTP " + response.status);
        });
      }
      return response.json();
    });
  }

  function cell(row, value, className) {
    var td = document.createElement("td");
    if (className) td.className = className;
    td.textContent = value;
    row.appendChild(td);
    return td;
  }

  function renderRows(data) {
    tbody.innerHTML = "";
    if (!data.items.length) {
      var emptyRow = document.createElement("tr");
      cell(emptyRow, "Không có Volume phù hợp trong pool này.", "hint").colSpan = 6;
      tbody.appendChild(emptyRow);
    }
    data.items.forEach(function (item) {
      var row = document.createElement("tr");
      var nameCell = cell(row, item.name);
      var code = document.createElement("code");
      code.textContent = item.name;
      nameCell.textContent = "";
      nameCell.appendChild(code);
      cell(row, item.image_id || "—");
      cell(row, bytes(item.used_size));
      cell(row, bytes(item.provisioned_size));
      cell(row, String(item.snapshot_count || 0));
      var action = cell(row, "");
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-ghost btn-sm";
      button.textContent = "Chi tiết";
      button.addEventListener("click", function () { loadDetail(item.name); });
      action.appendChild(button);
      tbody.appendChild(row);
    });
    state.page = data.page;
    state.pages = data.pages;
    pager.hidden = data.total <= data.page_size;
    pageStatus.textContent = "Trang " + data.page + " / " + data.pages + " · " + data.total + " Volume";
    prev.disabled = data.page <= 1;
    next.disabled = data.page >= data.pages;
    freshness.textContent = "Cập nhật live: " + new Date(data.collected_at).toLocaleString("vi-VN") +
      " · Used " + bytes(data.summary.used_size) + " / " + bytes(data.summary.provisioned_size);
  }

  function loadInventory() {
    if (state.loading) return;
    state.loading = true;
    error.hidden = true;
    var params = new URLSearchParams({
      search: search.value.trim(), sort: sort.value, order: "asc",
      page: String(state.page), page_size: "25"
    });
    requestJson("/api/volumes/" + encodeURIComponent(pool) + "/inventory?" + params.toString())
      .then(renderRows)
      .catch(function (exc) {
        if (exc.message === "unauthenticated") return;
        error.textContent = exc.message;
        error.hidden = false;
        freshness.textContent = "Không lấy được dữ liệu live";
      })
      .finally(function () { state.loading = false; });
  }

  function addListSection(root, title, items, formatter) {
    var heading = document.createElement("h3");
    heading.textContent = title + " (" + items.length + ")";
    root.appendChild(heading);
    if (!items.length) {
      var empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "Không có.";
      root.appendChild(empty);
      return;
    }
    var list = document.createElement("ul");
    items.forEach(function (item) {
      var li = document.createElement("li");
      li.textContent = formatter(item);
      list.appendChild(li);
    });
    root.appendChild(list);
  }

  function renderDetail(data) {
    detail.innerHTML = "";
    detail.className = "card-body";
    detail.hidden = false;
    var title = document.createElement("h3");
    title.textContent = data.pool + "/" + data.name;
    detail.appendChild(title);
    var summary = document.createElement("p");
    summary.className = "hint";
    summary.textContent = "ID: " + (data.image_id || "—") + " · Size: " + bytes(data.size) +
      " · Object: " + data.object_count + " × " + bytes(data.object_size) +
      " · Format: " + (data.format || "—") + " · Features: " + ((data.features || []).join(", ") || "—");
    detail.appendChild(summary);
    if (data.parent) {
      var parent = document.createElement("p");
      parent.textContent = "Parent: " + (typeof data.parent === "string" ? data.parent : JSON.stringify(data.parent));
      detail.appendChild(parent);
    }
    addListSection(detail, "Snapshot", data.snapshots || [], function (item) {
      return String(item.name || item.snap_name || item.id || "snapshot") +
        (item.size ? " · " + bytes(item.size) : "");
    });
    addListSection(detail, "Watcher / Attachment", data.watchers || [], function (item) {
      return [item.address, item.client, item.cookie].filter(Boolean).join(" · ") || JSON.stringify(item);
    });
    addListSection(detail, "Children / Clone", data.children || [], function (item) {
      return typeof item === "string" ? item : String(item.pool || "") + "/" + String(item.image || item.name || "");
    });
    detail.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }

  function loadDetail(image) {
    detail.hidden = false;
    detail.className = "empty-node-state";
    detail.textContent = "Đang tải chi tiết " + image + "…";
    requestJson(
      "/api/volumes/" + encodeURIComponent(pool) + "/inventory/" + encodeURIComponent(image)
    ).then(renderDetail).catch(function (exc) {
      if (exc.message === "unauthenticated") return;
      detail.textContent = "Không đọc được chi tiết: " + exc.message;
    });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    state.page = 1;
    loadInventory();
  });
  prev.addEventListener("click", function () { if (state.page > 1) { state.page -= 1; loadInventory(); } });
  next.addEventListener("click", function () { if (state.page < state.pages) { state.page += 1; loadInventory(); } });
  loadInventory();
}());
