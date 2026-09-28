(function () {
  "use strict";

  var panel = document.getElementById("volume-inventory-panel");
  if (!panel) return;

  var pool = panel.dataset.pool;
  var selectedImage = panel.dataset.image;
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
  var insightsStatus = document.getElementById("volume-ai-insights-status");
  var insightsGaps = document.getElementById("volume-ai-insights-gaps");
  var insightsList = document.getElementById("volume-ai-insights-list");
  var cinderMappingPanel = document.getElementById("volume-cinder-mapping");
  var cinderMappingRefresh = document.getElementById("volume-cinder-mapping-refresh");
  var cinderMappingStatus = document.getElementById("volume-cinder-mapping-status");
  var cinderMappingSummary = document.getElementById("volume-cinder-mapping-summary");
  var cinderMappingTable = document.getElementById("volume-cinder-mapping-table-wrap");
  var cinderMappingBody = document.getElementById("volume-cinder-mapping-body");
  var state = { page: 1, pages: 1, loading: false };
  var PAGE_SIZE = 10;

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
      row.className = "volume-inventory-row";
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
      button.textContent = "Xem chi tiết";
      button.addEventListener("click", function () { loadDetail(item.name); });
      action.appendChild(button);
      tbody.appendChild(row);
    });
    state.page = data.page;
    state.pages = data.pages;
    pager.hidden = data.total <= data.page_size;
    pageStatus.textContent = "Trang " + data.page + " / " + data.pages + " · " + data.total + " volumes · 10 dòng/trang";
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
      page: String(state.page), page_size: String(PAGE_SIZE)
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
    var bootDependency = data.boot_dependency || {};
    if (bootDependency.status && bootDependency.status !== "not_applicable") {
      var bootGuard = document.createElement("p");
      bootGuard.className = bootDependency.status === "ok" ? "hint" : "error";
      bootGuard.textContent = "Boot dependency: " +
        (bootDependency.boot_volume && bootDependency.boot_volume.bootable ? "boot volume" : "data volume") +
        " · Bảo vệ xoá trực tiếp: " +
        (bootDependency.guards && bootDependency.guards.protect_boot_volume ? "bật" : "không áp dụng") +
        " · Mutation: " + (bootDependency.mutation_supported ? "cho phép" : "read-only");
      detail.appendChild(bootGuard);
      (bootDependency.evidence_gaps || []).forEach(function (gap) {
        var gapText = document.createElement("p");
        gapText.className = "error";
        gapText.textContent = "Boot evidence: " + gap;
        detail.appendChild(gapText);
      });
    }
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
      var cinderSnapshots = data.cinder_snapshots || { items: [] };
      if (cinderSnapshots.status === "ok") {
        addListSection(detail, "Cinder Snapshot", cinderSnapshots.items || [], function (item) {
          return [item.name || item.snapshot_id, item.status, item.size_gib ? item.size_gib + " GiB" : "", item.created_at]
            .filter(Boolean).join(" · ");
        });
      } else if (cinderSnapshots.status !== "not_applicable") {
        var snapshotWarning = document.createElement("p");
        snapshotWarning.className = "error";
        snapshotWarning.textContent = "Cinder snapshot inventory: " + (cinderSnapshots.error || cinderSnapshots.status);
        detail.appendChild(snapshotWarning);
      }
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
      if (cinder.status === "managed" && reconciliation.status === "healthy") {
        if ((data.cinder_snapshots || {}).status === "ok") {
          var snapshotForm = document.createElement("form");
          snapshotForm.className = "audit-filters";
          var snapshotLabel = document.createElement("label");
          snapshotLabel.textContent = "Tên snapshot crash-consistent";
          var snapshotInput = document.createElement("input");
          snapshotInput.type = "text";
          snapshotInput.required = true;
          snapshotInput.maxLength = 128;
          snapshotInput.pattern = "[A-Za-z0-9][A-Za-z0-9_.-]*";
          snapshotLabel.appendChild(snapshotInput);
          snapshotForm.appendChild(snapshotLabel);
          var snapshotSubmit = document.createElement("button");
          snapshotSubmit.type = "submit";
          snapshotSubmit.className = "btn btn-primary btn-sm";
          snapshotSubmit.textContent = "Đề xuất tạo snapshot";
          snapshotForm.appendChild(snapshotSubmit);
          if (cinder.volume_status === "in-use") {
            var snapshotHint = document.createElement("span");
            snapshotHint.className = "error";
            snapshotHint.textContent = " Volume đang attached; Cinder sẽ tạo snapshot với --force.";
            snapshotForm.appendChild(snapshotHint);
          }
          snapshotForm.addEventListener("submit", function (event) {
            event.preventDefault();
            proposeMutation(
              "/api/volumes/" + encodeURIComponent(pool) + "/inventory/" + encodeURIComponent(data.name) + "/snapshots",
              { snapshot_name: snapshotInput.value.trim() }, snapshotSubmit
            );
          });
          detail.appendChild(snapshotForm);
        }
        var canAttach = cinder.volume_status === "available" ||
          (cinder.volume_status === "in-use" && cinder.multiattach === true);
        if (canAttach) {
          var attachForm = document.createElement("form");
          attachForm.className = "audit-filters";
          var attachLabel = document.createElement("label");
          attachLabel.textContent = cinder.volume_status === "in-use" ? "Nova server UUID cần multi-attach" : "Nova server UUID cần attach";
          var attachInput = document.createElement("input");
          attachInput.type = "text";
          attachInput.required = true;
          attachInput.pattern = "[0-9a-fA-F-]{36}";
          attachLabel.appendChild(attachInput);
          attachForm.appendChild(attachLabel);
          var attachSubmit = document.createElement("button");
          attachSubmit.type = "submit";
          attachSubmit.className = "btn btn-primary btn-sm";
          attachSubmit.textContent = cinder.volume_status === "in-use" ? "Đề xuất multi-attach" : "Đề xuất attach qua Cinder";
          attachForm.appendChild(attachSubmit);
          attachForm.addEventListener("submit", function (event) {
            event.preventDefault();
            proposeMutation(
              "/api/volumes/" + encodeURIComponent(pool) + "/inventory/" + encodeURIComponent(data.name) + "/attach",
              { server_id: attachInput.value.trim() }, attachSubmit
            );
          });
          detail.appendChild(attachForm);
        }
        if ((cinder.attachments || []).length) {
          var detachForm = document.createElement("form");
          detachForm.className = "audit-filters";
          var detachLabel = document.createElement("label");
          detachLabel.textContent = "Nova server UUID cần detach";
          var detachSelect = document.createElement("select");
          (cinder.attachments || []).forEach(function (item) {
            var option = document.createElement("option");
            option.value = item.instance_id || "";
            option.textContent = [item.instance_id, item.host, item.device].filter(Boolean).join(" · ");
            detachSelect.appendChild(option);
          });
          detachLabel.appendChild(detachSelect);
          detachForm.appendChild(detachLabel);
          var detachSubmit = document.createElement("button");
          detachSubmit.type = "submit";
          detachSubmit.className = "btn btn-reject btn-sm";
          detachSubmit.textContent = "Đề xuất detach qua Cinder";
          detachForm.appendChild(detachSubmit);
          detachForm.addEventListener("submit", function (event) {
            event.preventDefault();
            proposeMutation(
              "/api/volumes/" + encodeURIComponent(pool) + "/inventory/" + encodeURIComponent(data.name) + "/detach",
              { server_id: detachSelect.value }, detachSubmit
            );
          });
          detail.appendChild(detachForm);
        }
      }
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

      var qosSection = document.createElement("section");
      qosSection.className = "volume-qos-editor";
      var qosTitle = document.createElement("h3");
      qosTitle.textContent = "QoS của Volume";
      qosSection.appendChild(qosTitle);
      var qosStatus = document.createElement("p");
      qosStatus.className = "hint";
      qosStatus.textContent = "Đang đọc giới hạn RBD…";
      qosSection.appendChild(qosStatus);
      var qosForm = document.createElement("form");
      qosForm.className = "audit-filters";
      var qosFields = [
        ["iops_limit", "IOPS limit"], ["bps_limit", "Throughput limit (B/s)"],
        ["iops_burst", "IOPS burst"], ["bps_burst", "Throughput burst (B/s)"]
      ];
      var qosInputs = {};
      qosFields.forEach(function (field) {
        var label = document.createElement("label");
        label.textContent = field[1];
        var input = document.createElement("input");
        input.type = "number";
        input.min = "0";
        input.max = "1000000000000000";
        input.placeholder = "Không đổi · 0 để bỏ giới hạn";
        label.appendChild(input);
        qosForm.appendChild(label);
        qosInputs[field[0]] = input;
      });
      var qosSubmit = document.createElement("button");
      qosSubmit.type = "submit";
      qosSubmit.className = "btn btn-primary btn-sm";
      qosSubmit.textContent = "Đề xuất thay đổi QoS";
      qosForm.appendChild(qosSubmit);
      qosForm.addEventListener("submit", function (event) {
        event.preventDefault();
        var payload = {};
        var changed = false;
        qosFields.forEach(function (field) {
          var raw = qosInputs[field[0]].value.trim();
          if (raw !== "") { payload[field[0]] = Number(raw); changed = true; }
        });
        if (!changed) { qosStatus.textContent = "Hãy nhập ít nhất một giới hạn QoS."; return; }
        proposeMutation(
          "/api/volumes/" + encodeURIComponent(pool) + "/inventory/" + encodeURIComponent(data.name) + "/qos",
          payload, qosSubmit
        );
      });
      qosSection.appendChild(qosForm);
      detail.appendChild(qosSection);
      requestJson("/api/volumes/" + encodeURIComponent(pool) + "/inventory/" + encodeURIComponent(data.name) + "/qos")
        .then(function (response) {
          var qos = response.qos || {};
          qosStatus.textContent = "Hiện tại: IOPS " + (qos.rbd_qos_iops_limit == null ? "không giới hạn" : qos.rbd_qos_iops_limit) +
            " · Throughput " + (qos.rbd_qos_bps_limit == null ? "không giới hạn" : bytes(qos.rbd_qos_bps_limit) + "/s");
        })
        .catch(function () { qosStatus.textContent = "Không đọc được QoS; backend có thể chưa hỗ trợ cấu hình image QoS."; });

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

  function renderInsights(data) {
    if (!insightsList) return;
    insightsList.innerHTML = "";
    var items = data.insights || [];
    if (!items.length) {
      var empty = document.createElement("p");
      empty.className = "hint";
      empty.textContent = "Chưa có cảnh báo inventory từ evidence hiện có.";
      insightsList.appendChild(empty);
    }
    items.forEach(function (item) {
      var card = document.createElement("article");
      card.className = "volume-ai-insight-item";
      var title = document.createElement("strong");
      title.textContent = item.kind + " · " + item.image;
      card.appendChild(title);
      var reason = document.createElement("p");
      reason.textContent = item.reason || "Không có lý do";
      card.appendChild(reason);
      var recommendation = document.createElement("p");
      recommendation.className = "hint";
      recommendation.textContent = "Đề xuất: " + (item.recommendation || "review evidence");
      card.appendChild(recommendation);
      var meta = document.createElement("small");
      meta.textContent = "Confidence: " + (item.confidence == null ? "—" : item.confidence) +
        " · Read-only · Evidence hết hạn: " + (item.evidence_expires_at || "—");
      card.appendChild(meta);
      insightsList.appendChild(card);
    });
    if (insightsStatus) {
      insightsStatus.textContent = String(data.summary && data.summary.total || 0) +
        " insight · chỉ đọc";
    }
    if (insightsGaps) {
      var gaps = data.evidence_gaps || [];
      insightsGaps.textContent = gaps.length ? "Giới hạn evidence: " + gaps.join(" · ") : "";
      insightsGaps.hidden = !gaps.length;
    }
  }

  function loadInsights() {
    if (!insightsList) return;
    requestJson("/api/volumes/" + encodeURIComponent(pool) + "/inventory-insights")
      .then(renderInsights)
      .catch(function (exc) {
        if (exc.message === "unauthenticated") return;
        insightsStatus.textContent = "Không lấy được insight";
        insightsList.innerHTML = "";
        var errorItem = document.createElement("p");
        errorItem.className = "error";
        errorItem.textContent = exc.message;
        insightsList.appendChild(errorItem);
      });
  }

  function renderCinderMapping(data) {
    if (!cinderMappingSummary || !cinderMappingBody) return;
    var summary = data.summary || {};
    cinderMappingSummary.innerHTML = "";
    [
      ["Mapped", summary.mapped],
      ["Cinder không có RBD", summary.cinder_without_rbd],
      ["RBD không có Cinder", summary.rbd_without_cinder],
      ["RBD native", summary.not_cinder_image]
    ].forEach(function (entry) {
      var item = document.createElement("div");
      item.className = "summary-item";
      var label = document.createElement("span");
      label.textContent = entry[0];
      var value = document.createElement("strong");
      value.textContent = String(entry[1] == null ? 0 : entry[1]);
      item.appendChild(label);
      item.appendChild(value);
      cinderMappingSummary.appendChild(item);
    });
    cinderMappingSummary.hidden = false;
    cinderMappingBody.innerHTML = "";
    (data.items || []).forEach(function (item) {
      var row = document.createElement("tr");
      var status = item.mapping_status || "unknown";
      row.className = "cinder-mapping-row cinder-mapping-" + status.replace(/[^a-z0-9_-]/gi, "-");
      cell(row, item.image || "—");
      cell(row, [item.name || item.volume_id, item.project_id ? "Project " + item.project_id : ""]
        .filter(Boolean).join(" · ") || "—");
      var attachmentText = (item.attachments || []).map(function (attachment) {
        return [attachment.instance_id, attachment.host, attachment.device].filter(Boolean).join(" · ");
      }).filter(Boolean).join("; ") || item.attachment_summary || "—";
      cell(row, attachmentText);
      var statusCell = cell(row, "");
      var badge = document.createElement("span");
      badge.className = "cinder-mapping-badge";
      badge.textContent = {
        mapped: "Đã map",
        cinder_without_rbd: "Thiếu RBD",
        rbd_without_cinder: "Orphan RBD",
        not_cinder_managed: "RBD native"
      }[status] || status;
      statusCell.appendChild(badge);
      cell(row, (item.source_of_truth || "unknown") + " / " + (item.mutation_route || "blocked"));
      cinderMappingBody.appendChild(row);
    });
    cinderMappingTable.hidden = false;
    cinderMappingStatus.textContent = "Đã đối soát " + Number(summary.total_rbd || 0) +
      " RBD image với " + Number(summary.total_cinder || 0) + " Cinder volume · " +
      new Date(data.collected_at).toLocaleString("vi-VN");
  }

  function loadCinderMapping() {
    if (!cinderMappingPanel || !cinderMappingRefresh) return;
    cinderMappingRefresh.disabled = true;
    cinderMappingStatus.textContent = "Đang đọc inventory Cinder và RBD…";
    cinderMappingSummary.hidden = true;
    cinderMappingTable.hidden = true;
    requestJson("/api/volumes/" + encodeURIComponent(pool) + "/cinder-mapping")
      .then(renderCinderMapping)
      .catch(function (exc) {
        if (exc.message === "unauthenticated") return;
        cinderMappingStatus.textContent = "Không đối soát được Cinder: " + exc.message;
      })
      .finally(function () { cinderMappingRefresh.disabled = false; });
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
  if (cinderMappingRefresh) cinderMappingRefresh.addEventListener("click", loadCinderMapping);
  loadOverview();
  loadInsights();
  loadInventory();
  if (selectedImage) loadDetail(selectedImage);
}());
