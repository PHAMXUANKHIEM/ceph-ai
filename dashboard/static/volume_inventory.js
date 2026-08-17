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
  var createForm = document.getElementById("volume-create-form");
  var mutationResult = document.getElementById("volume-mutation-result");
  var isAdmin = panel.dataset.isAdmin === "true";
  var overview = document.getElementById("volume-pool-overview");
  var overviewError = document.getElementById("volume-pool-overview-error");
  var healthChecks = document.getElementById("volume-pool-health-checks");
  var state = { page: 1, pages: 1, loading: false };

  function bytes(value) {
    var n = Number(value || 0);
    var units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    var i = 0;
    while (n >= 1024 && i < units.length - 1) { n /= 1024; i += 1; }
    return (i === 0 ? n.toFixed(0) : n.toFixed(1)) + " " + units[i];
  }

  function requestJson(url, options) {
    var cluster = new URLSearchParams(window.location.search).get("cluster");
    if (cluster) {
      var scoped = new URL(url, window.location.origin);
      scoped.searchParams.set("cluster", cluster);
      url = scoped.pathname + scoped.search;
    }
    var requestOptions = options || {};
    requestOptions.credentials = "same-origin";
    return fetch(url, requestOptions).then(function (response) {
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
    var partialErrors = data.partial_errors || {};
    if (Object.keys(partialErrors).length) {
      var warning = document.createElement("p");
      warning.className = "error";
      warning.textContent = "Một số mục không đọc được: " + Object.keys(partialErrors).join(", ");
      detail.appendChild(warning);
    }
    if (data.parent) {
      var parent = document.createElement("p");
      parent.textContent = "Parent: " + (typeof data.parent === "string" ? data.parent : JSON.stringify(data.parent));
      detail.appendChild(parent);
    }
    addListSection(detail, "Snapshot", data.snapshots || [], function (item) {
      return String(item.name || item.snap_name || item.id || "snapshot") +
        (item.size ? " · " + bytes(item.size) : "");
    });
    var attachment = data.attachment_summary || {};
    var reconciliation = data.attachment_reconciliation || {};
    var attachmentGuard = document.createElement("p");
    attachmentGuard.className = reconciliation.status === "healthy" ? "hint" : "error";
    attachmentGuard.textContent = "Attachment guard: " +
      (attachment.attached ? "đang có consumer" : "không phát hiện consumer") +
      " · Watcher: " + Number(attachment.watcher_count || 0) +
      " · Lock: " + Number(attachment.lock_count || 0) +
      " · Control plane: " + (attachment.management_source || "unknown") +
      " · Reconcile: " + (reconciliation.status || "unknown") +
      " · Attach/detach trực tiếp: " + (attachment.mutation_supported ? "cho phép" : "đã khóa");
    detail.appendChild(attachmentGuard);
    if (reconciliation.reason) {
      var reconcileReason = document.createElement("p");
      reconcileReason.className = "error";
      reconcileReason.textContent = "Đối soát attachment: " + reconciliation.reason;
      detail.appendChild(reconcileReason);
    }
    var cinder = data.cinder || {};
    if (cinder.status === "managed") {
      var cinderSummary = document.createElement("p");
      cinderSummary.className = "hint";
      cinderSummary.textContent = "Cinder: " + cinder.volume_id +
        " · Status: " + (cinder.volume_status || "—") +
        " · Project: " + (cinder.project_id || "—") +
        " · Type: " + (cinder.volume_type || "—") +
        " · Multiattach: " + (cinder.multiattach ? "có" : "không");
      detail.appendChild(cinderSummary);
      addListSection(detail, "Cinder Attachment", cinder.attachments || [], function (item) {
        return [item.attachment_id, item.instance_id, item.host, item.device].filter(Boolean).join(" · ") || JSON.stringify(item);
      });
    } else if (["error", "not_configured", "not_found"].indexOf(cinder.status) !== -1) {
      var cinderWarning = document.createElement("p");
      cinderWarning.className = "error";
      cinderWarning.textContent = "Cinder discovery: " + (cinder.error || cinder.status);
      detail.appendChild(cinderWarning);
    }
    addListSection(detail, "Watcher", data.watchers || [], function (item) {
      return [item.address, item.client, item.cookie].filter(Boolean).join(" · ") || JSON.stringify(item);
    });
    addListSection(detail, "Lock", data.locks || [], function (item) {
      return [item.locker_id, item.locker, item.client, item.address, item.cookie, item.description]
        .filter(Boolean).join(" · ") || JSON.stringify(item);
    });
    addListSection(detail, "Children / Clone", data.children || [], function (item) {
      return typeof item === "string" ? item : String(item.pool || "") + "/" + String(item.image || item.name || "");
    });
    if (isAdmin) {
      var resizeForm = document.createElement("form");
      resizeForm.className = "audit-filters";
      var label = document.createElement("label");
      label.textContent = "Mở rộng tới (GiB)";
      var input = document.createElement("input");
      input.type = "number";
      input.min = "1";
      input.max = "65536";
      input.required = true;
      input.value = String(Math.max(1, Math.ceil(Number(data.size || 0) / Math.pow(1024, 3)) + 1));
      label.appendChild(input);
      resizeForm.appendChild(label);
      var submit = document.createElement("button");
      submit.type = "submit";
      submit.className = "btn btn-primary btn-sm";
      submit.textContent = "Đề xuất mở rộng";
      resizeForm.appendChild(submit);
      resizeForm.addEventListener("submit", function (event) {
        event.preventDefault();
        proposeMutation(
          "/api/volumes/" + encodeURIComponent(pool) + "/inventory/" + encodeURIComponent(data.name) + "/resize",
          { size_gib: Number(input.value) }, submit
        );
      });
      detail.appendChild(resizeForm);

      var renameForm = document.createElement("form");
      renameForm.className = "audit-filters";
      var renameLabel = document.createElement("label");
      renameLabel.textContent = "Tên Volume mới";
      var renameInput = document.createElement("input");
      renameInput.type = "text";
      renameInput.required = true;
      renameInput.maxLength = 128;
      renameInput.pattern = "[A-Za-z0-9][A-Za-z0-9_.-]*";
      renameInput.value = data.name;
      renameLabel.appendChild(renameInput);
      renameForm.appendChild(renameLabel);
      var renameSubmit = document.createElement("button");
      renameSubmit.type = "submit";
      renameSubmit.className = "btn btn-primary btn-sm";
      renameSubmit.textContent = "Đề xuất đổi tên";
      renameForm.appendChild(renameSubmit);
      var renameHint = document.createElement("span");
      renameHint.className = "hint";
      renameHint.textContent = " Cần detach consumer; thao tác có thể yêu cầu downtime.";
      renameForm.appendChild(renameHint);
      renameForm.addEventListener("submit", function (event) {
        event.preventDefault();
        proposeMutation(
          "/api/volumes/" + encodeURIComponent(pool) + "/inventory/" + encodeURIComponent(data.name) + "/rename",
          { new_image: renameInput.value.trim() }, renameSubmit
        );
      });
      detail.appendChild(renameForm);

      var trashButton = document.createElement("button");
      trashButton.type = "button";
      trashButton.className = "btn btn-reject btn-sm";
      trashButton.textContent = "Đề xuất chuyển vào Trash";
      trashButton.addEventListener("click", function () {
        if (!window.confirm("Chuyển " + data.pool + "/" + data.name + " vào Trash? Volume phải không còn watcher, snapshot hoặc clone child.")) return;
        proposeMutation(
          "/api/volumes/" + encodeURIComponent(pool) + "/inventory/" + encodeURIComponent(data.name) + "/trash",
          {}, trashButton
        );
      });
      detail.appendChild(trashButton);
    }
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

  function setOverview(field, value) {
    var target = overview.querySelector('[data-field="' + field + '"]');
    if (target) target.textContent = value;
  }

  function loadOverview() {
    requestJson("/api/volumes/" + encodeURIComponent(pool) + "/inventory-overview")
      .then(function (data) {
        var durability = data.type === "erasure"
          ? "EC " + (data.erasure_code_profile || "—")
          : "Replica " + data.replica_size + " / min " + data.min_size;
        setOverview("type", data.type || "—");
        setOverview("durability", durability);
        setOverview("pg", String(data.pg_num || 0) + " / PGP " + String(data.pgp_num || 0));
        setOverview("physical", bytes(data.bytes_used) + " · " + Number(data.percent_used || 0).toFixed(1) + "%");
        setOverview("rbd", data.rbd_enabled ? "Enabled" : "Disabled");
        setOverview("health", data.near_full ? "⚠ Near full" : (data.health || "unknown"));
        healthChecks.textContent = (data.health_checks || []).map(function (item) {
          return item.code + ": " + item.summary;
        }).join(" · ");
      })
      .catch(function (exc) {
        if (exc.message === "unauthenticated") return;
        overviewError.textContent = exc.message;
        overviewError.hidden = false;
      });
  }

  function proposeMutation(url, payload, button) {
    button.disabled = true;
    var idempotencyKey = (window.crypto && window.crypto.randomUUID)
      ? window.crypto.randomUUID()
      : "ui-" + Date.now() + "-" + Math.random().toString(16).slice(2);
    requestJson(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(payload)
    }).then(function (data) {
      mutationResult.hidden = false;
      mutationResult.className = "success";
      mutationResult.textContent = "Đã tạo đề xuất " + data.action_id + ". Hãy duyệt trong Dashboard/Audit Trail.";
    }).catch(function (exc) {
      if (exc.message === "unauthenticated") return;
      mutationResult.hidden = false;
      mutationResult.className = "error";
      mutationResult.textContent = exc.message;
    }).finally(function () { button.disabled = false; });
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    state.page = 1;
    loadInventory();
  });
  if (createForm) {
    createForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var button = createForm.querySelector('button[type="submit"]');
      proposeMutation(
        "/api/volumes/" + encodeURIComponent(pool) + "/inventory/create",
        {
          image: createForm.elements.image.value.trim(),
          size_gib: Number(createForm.elements.size_gib.value)
        },
        button
      );
    });
  }
  prev.addEventListener("click", function () { if (state.page > 1) { state.page -= 1; loadInventory(); } });
  next.addEventListener("click", function () { if (state.page < state.pages) { state.page += 1; loadInventory(); } });
  loadOverview();
  loadInventory();
}());
