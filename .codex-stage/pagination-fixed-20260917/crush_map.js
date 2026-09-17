(function () {
  "use strict";

  // CRUSH distribution changes less frequently than health data. Keep the
  // existing lightweight poll, but only rebuild the selected root when the
  // snapshot payload changes.
  var POLL_INTERVAL_MS = 15000;
  var COLLAPSED_STORAGE_KEY = "crushMapCollapsedNodes";
  var CRUSH_WEIGHT_SCALE = 65536;
  var treeEl = document.getElementById("crush-map-tree");
  var noSnapshotEl = document.getElementById("crush-map-empty-no-snapshot");
  var emptyClusterEl = document.getElementById("crush-map-empty-cluster");
  var errorEl = document.getElementById("crush-map-error");
  var metaEl = document.getElementById("crush-map-meta");
  var rulesEl = document.getElementById("crush-rules-list");
  var rulesEmptyEl = document.getElementById("crush-rules-empty");
  var snapshotBadgeEl = document.getElementById("crush-snapshot-badge");
  var refreshBtn = document.getElementById("crush-tree-refresh");
  var rootPickerWrap = document.getElementById("crush-root-picker-wrap");
  var rootPicker = document.getElementById("crush-root-picker");

  if (!treeEl) return;

  var clusterId = treeEl.dataset.clusterId || "";
  var rootItems = [];
  var selectedRootIndex = 0;

  function loadCollapsed() {
    try {
      var raw = localStorage.getItem(COLLAPSED_STORAGE_KEY);
      return raw ? new Set(JSON.parse(raw)) : new Set();
    } catch (e) {
      return new Set();
    }
  }

  function saveCollapsed(collapsedSet) {
    try {
      localStorage.setItem(COLLAPSED_STORAGE_KEY, JSON.stringify(Array.from(collapsedSet)));
    } catch (e) { /* private mode or quota — state remains in this render */ }
  }

  function fmtWeight(value) {
    return typeof value === "number" ? (value / CRUSH_WEIGHT_SCALE).toFixed(3) : "—";
  }

  function nodeType(node) {
    return String(node.type || "unknown").toLowerCase();
  }

  function nodeIcon(type) {
    if (type === "root") return "⌂";
    if (type === "host") return "▦";
    if (type === "osd") return "◉";
    return "•";
  }

  function utilization(node) {
    if (!node.has_distribution_data || typeof node.bytes_total !== "number" || node.bytes_total <= 0) return null;
    return Math.max(0, Math.min(100, (node.bytes_used / node.bytes_total) * 100));
  }

  function utilizationClass(value) {
    if (value == null) return "is-unknown";
    if (value > 80) return "is-critical";
    if (value >= 60) return "is-warning";
    return "is-healthy";
  }

  function appendMetric(parent, label, value, className) {
    var item = document.createElement("span");
    item.className = "crush-metric " + (className || "");
    var labelEl = document.createElement("span");
    labelEl.className = "crush-metric-label";
    labelEl.textContent = label;
    var valueEl = document.createElement("strong");
    valueEl.textContent = value;
    item.appendChild(labelEl);
    item.appendChild(valueEl);
    parent.appendChild(item);
  }

  function buildUsage(parent, node) {
    var value = utilization(node);
    var usage = document.createElement("div");
    usage.className = "crush-utilization " + utilizationClass(value);
    var head = document.createElement("div");
    head.className = "crush-utilization-head";
    var label = document.createElement("span");
    label.textContent = "Utilization";
    var valueEl = document.createElement("strong");
    valueEl.textContent = value == null ? "Chưa có dữ liệu" : value.toFixed(1) + "%";
    head.appendChild(label);
    head.appendChild(valueEl);
    usage.appendChild(head);
    var track = document.createElement("span");
    track.className = "crush-utilization-track";
    var fill = document.createElement("span");
    fill.className = "crush-utilization-fill";
    fill.style.width = value == null ? "0%" : value.toFixed(1) + "%";
    track.appendChild(fill);
    usage.appendChild(track);
    parent.appendChild(usage);
  }

  function buildNodeEl(node, collapsedSet, depth) {
    var wrap = document.createElement("div");
    var type = nodeType(node);
    wrap.className = "crush-node crush-node--" + type;
    wrap.dataset.nodeId = String(node.id == null ? (node.name || "unknown") : node.id);

    var hasChildren = Array.isArray(node.children) && node.children.length > 0;
    var nodeKey = String(node.id);
    var isCollapsed = hasChildren && collapsedSet.has(nodeKey);
    var row = document.createElement("div");
    row.className = "crush-node-row";

    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "crush-node-toggle";
    toggle.disabled = !hasChildren;
    toggle.setAttribute("aria-label", hasChildren ? (isCollapsed ? "Mở nhánh " : "Thu gọn nhánh ") + (node.name || "node") : "Node lá");
    toggle.setAttribute("aria-expanded", String(!isCollapsed));
    toggle.textContent = hasChildren ? (isCollapsed ? "▸" : "▾") : "";
    row.appendChild(toggle);

    var icon = document.createElement("span");
    icon.className = "crush-node-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = nodeIcon(type);
    row.appendChild(icon);

    var main = document.createElement("div");
    main.className = "crush-node-main";
    var titleLine = document.createElement("div");
    titleLine.className = "crush-node-title-line";
    var typeEl = document.createElement("span");
    typeEl.className = "crush-node-type";
    typeEl.textContent = type.toUpperCase();
    titleLine.appendChild(typeEl);
    var nameEl = document.createElement("strong");
    nameEl.className = "crush-node-name";
    nameEl.textContent = node.name || ("#" + node.id);
    nameEl.title = node.name || ("#" + node.id);
    titleLine.appendChild(nameEl);
    main.appendChild(titleLine);

    var metrics = document.createElement("div");
    metrics.className = "crush-node-metrics";
    appendMetric(metrics, "Weight", node.weight_normalized == null ? "—" : Number(node.weight_normalized).toFixed(3), "is-mono");
    appendMetric(metrics, "PG", typeof node.pgs === "number" ? String(node.pgs) : "—", "is-mono");
    if (node.partial_distribution_data) {
      var partial = document.createElement("span");
      partial.className = "crush-partial-note";
      partial.textContent = "Một phần dữ liệu";
      metrics.appendChild(partial);
    }
    main.appendChild(metrics);
    row.appendChild(main);

    if (type === "osd") buildUsage(row, node);

    if (node.recent_change) {
      var badge = document.createElement("span");
      var kind = node.recent_change.kind;
      badge.className = "crush-node-badge " + (kind === "added" ? "is-added" : "is-reweighted");
      badge.textContent = kind === "added"
        ? "Mới thêm"
        : "Weight " + fmtWeight(node.recent_change.old_weight) + " → " + fmtWeight(node.recent_change.new_weight);
      badge.title = node.recent_change.changed_at ? "Thay đổi lúc " + new Date(node.recent_change.changed_at).toLocaleString("vi-VN") : "";
      row.appendChild(badge);
    }
    wrap.appendChild(row);

    if (hasChildren) {
      var children = document.createElement("div");
      children.className = "crush-node-children";
      children.hidden = isCollapsed;
      node.children.forEach(function (child) {
        children.appendChild(buildNodeEl(child, collapsedSet, depth + 1));
      });
      wrap.appendChild(children);
      toggle.addEventListener("click", function () {
        var nextCollapsed = !children.hidden;
        children.hidden = nextCollapsed;
        toggle.setAttribute("aria-expanded", String(!nextCollapsed));
        toggle.textContent = nextCollapsed ? "▸" : "▾";
        if (nextCollapsed) collapsedSet.add(nodeKey); else collapsedSet.delete(nodeKey);
        saveCollapsed(collapsedSet);
      });
    }
    return wrap;
  }

  function hideAllStates() {
    [treeEl, noSnapshotEl, emptyClusterEl, errorEl, metaEl].forEach(function (el) {
      if (el) el.hidden = true;
    });
  }

  function setSnapshotStatus(meta) {
    if (!snapshotBadgeEl) return;
    var stale = !meta || meta.stale;
    snapshotBadgeEl.hidden = !meta || !meta.available;
    snapshotBadgeEl.className = "crush-snapshot-badge " + (stale ? "is-stale" : "is-live");
    snapshotBadgeEl.textContent = stale ? "⚠ Dữ liệu cũ" : "● Live";
    snapshotBadgeEl.title = stale ? "Snapshot đã quá thời hạn mới nhất" : "Snapshot đang trong thời hạn";
  }

  function renderRootPicker(roots) {
    rootItems = roots || [];
    if (!rootPicker || !rootPickerWrap) return;
    rootPicker.replaceChildren();
    rootItems.forEach(function (root, index) {
      var option = document.createElement("option");
      option.value = String(index);
      option.textContent = (root.name || ("Root #" + root.id)) + (root.children && root.children.length ? " · " + root.children.length + " nhánh" : "");
      rootPicker.appendChild(option);
    });
    selectedRootIndex = Math.min(selectedRootIndex, Math.max(0, rootItems.length - 1));
    rootPicker.value = String(selectedRootIndex);
    rootPickerWrap.hidden = rootItems.length <= 1;
  }

  function renderSelectedRoot() {
    if (!rootItems.length) return;
    var collapsedSet = loadCollapsed();
    treeEl.replaceChildren(buildNodeEl(rootItems[selectedRootIndex], collapsedSet, 0));
    treeEl.hidden = false;
  }

  function renderRules(rules) {
    if (!rulesEl || !rulesEmptyEl) return;
    rulesEl.replaceChildren();
    var hasRules = Array.isArray(rules) && rules.length > 0;
    rulesEmptyEl.hidden = hasRules;
    rulesEl.hidden = !hasRules;
    (rules || []).forEach(function (rule) {
      var card = document.createElement("article");
      card.className = "crush-rule";
      var header = document.createElement("div");
      header.className = "crush-rule-header";
      var title = document.createElement("div");
      title.className = "crush-rule-title";
      var name = document.createElement("strong");
      name.textContent = rule.rule_name || ("Rule #" + rule.rule_id);
      title.appendChild(name);
      var id = document.createElement("span");
      id.className = "crush-rule-id";
      id.textContent = "ID " + (rule.rule_id == null ? "—" : rule.rule_id);
      title.appendChild(id);
      header.appendChild(title);
      var tags = document.createElement("div");
      tags.className = "crush-rule-tags";
      [["type", rule.type], ["min", rule.min_size], ["max", rule.max_size]].forEach(function (pair) {
        var tag = document.createElement("span");
        tag.className = "crush-rule-tag";
        tag.textContent = pair[0] + " " + (pair[1] == null ? "—" : pair[1]);
        tags.appendChild(tag);
      });
      header.appendChild(tags);
      card.appendChild(header);

      var steps = document.createElement("ol");
      steps.className = "crush-rule-steps";
      (rule.steps || []).forEach(function (step, index) {
        var item = document.createElement("li");
        item.className = "crush-rule-step";
        var number = document.createElement("span");
        number.className = "crush-rule-step-number";
        number.textContent = String(index + 1).padStart(2, "0");
        item.appendChild(number);
        var copy = document.createElement("div");
        copy.className = "crush-rule-step-copy";
        var op = document.createElement("strong");
        op.textContent = step.op || "?";
        copy.appendChild(op);
        var parameters = [];
        if (step.item_name) parameters.push(step.item_name);
        else if (typeof step.item === "number") parameters.push("item=" + step.item);
        if (typeof step.num === "number") parameters.push("num=" + step.num);
        if (step.type) parameters.push("type=" + step.type);
        if (parameters.length) {
          var detail = document.createElement("span");
          detail.textContent = parameters.join(" · ");
          copy.appendChild(detail);
        }
        item.appendChild(copy);
        steps.appendChild(item);
      });
      card.appendChild(steps);
      rulesEl.appendChild(card);
    });
  }

  function renderTree(data) {
    hideAllStates();
    var meta = data.meta || {};
    setSnapshotStatus(meta);
    var collectedAt = meta.collected_at || data.created_at;
    var metaText = collectedAt ? "Snapshot lúc " + new Date(collectedAt).toLocaleString("vi-VN") : "Chưa có snapshot";
    if (meta.stale) metaText += " · dữ liệu cũ";
    if (meta.last_error) metaText += " · lỗi: " + meta.last_error;
    metaEl.textContent = metaText;
    metaEl.hidden = false;

    if (data.state === "no_snapshot_yet") {
      renderRules([]);
      renderRootPicker([]);
      setSnapshotStatus({ available: false, stale: true });
      noSnapshotEl.hidden = false;
      return;
    }

    renderRules(data.rules || []);
    if (data.state === "empty_cluster") {
      renderRootPicker([]);
      emptyClusterEl.hidden = false;
      return;
    }

    var roots = Array.isArray(data.roots) ? data.roots : [];
    renderRootPicker(roots);
    if (!roots.length) {
      emptyClusterEl.hidden = false;
      return;
    }
    renderSelectedRoot();
  }

  var lastPayload = null;
  var pollTimer = null;
  var requestInFlight = false;

  function schedulePoll() {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(poll, POLL_INTERVAL_MS);
  }

  function poll() {
    if (document.hidden || requestInFlight) {
      schedulePoll();
      return;
    }
    requestInFlight = true;
    if (refreshBtn) refreshBtn.disabled = true;
    var url = "/api/crush-map/tree?cluster_id=" + encodeURIComponent(clusterId);
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        errorEl.hidden = true;
        var payload = JSON.stringify(data);
        if (payload !== lastPayload) {
          renderTree(data);
          lastPayload = payload;
        }
      })
      .catch(function () {
        errorEl.hidden = false;
        if (snapshotBadgeEl) {
          snapshotBadgeEl.hidden = false;
          snapshotBadgeEl.className = "crush-snapshot-badge is-stale";
          snapshotBadgeEl.textContent = "⚠ Không đồng bộ";
        }
      })
      .finally(function () {
        requestInFlight = false;
        if (refreshBtn) refreshBtn.disabled = false;
        schedulePoll();
      });
  }

  if (rootPicker) rootPicker.addEventListener("change", function () {
    selectedRootIndex = Number(rootPicker.value) || 0;
    renderSelectedRoot();
  });
  if (refreshBtn) refreshBtn.addEventListener("click", function () {
    lastPayload = null;
    poll();
  });
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && !requestInFlight) {
      window.clearTimeout(pollTimer);
      poll();
    }
  });
  poll();
})();

(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-crush-tab]"));
  if (!tabs.length) return;
  var panels = Array.prototype.slice.call(document.querySelectorAll(".crush-feature-panel, .bucket-feature-panel"));
  function activate(tab) {
    tabs.forEach(function (item) {
      var active = item === tab;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-selected", String(active));
      item.tabIndex = active ? 0 : -1;
    });
    panels.forEach(function (panel) {
      if (panel.id.indexOf("crush-") === 0) panel.hidden = panel.id !== tab.dataset.crushTab;
    });
  }
  tabs.forEach(function (tab, index) {
    tab.addEventListener("click", function () { activate(tab); });
    tab.addEventListener("keydown", function (event) {
      if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
      event.preventDefault();
      var offset = event.key === "ArrowRight" ? 1 : -1;
      var next = tabs[(index + offset + tabs.length) % tabs.length];
      activate(next);
      next.focus();
    });
  });
  activate(tabs[0]);
})();

(function () {
  var listEl = document.getElementById("crush-history-list");
  if (!listEl) return;
  var emptyEl = document.getElementById("crush-history-empty");
  var pageEl = document.getElementById("crush-history-page");
  var countEl = document.getElementById("crush-history-count");
  var pagesEl = document.getElementById("crush-history-pages");
  var prevBtn = document.getElementById("crush-history-prev");
  var nextBtn = document.getElementById("crush-history-next");
  var purgeBtn = document.getElementById("crush-history-purge");
  var detailEl = document.getElementById("crush-history-detail");
  var detailTitleEl = document.getElementById("crush-history-detail-title");
  var detailBodyEl = document.getElementById("crush-history-detail-body");
  var detailCloseBtn = document.getElementById("crush-history-detail-close");
  var treePageEl = document.getElementById("crush-map-tree");
  var clusterId = treePageEl ? treePageEl.dataset.clusterId : "";
  var currentPage = 1;
  var pageCursors = [null];
  var historyTotal = 0;
  var historyPages = 1;
  var historyPageSize = 10;
  var requestInFlight = false;
  var CRUSH_WEIGHT_SCALE = 65536;

  function fmtTime(iso) { return new Date(iso).toLocaleString("vi-VN"); }
  function fmtWeight(value) { return typeof value === "number" ? (value / CRUSH_WEIGHT_SCALE).toFixed(3) : "—"; }

  function renderDiffGroup(title, items, formatter, className) {
    if (!items || !items.length) return null;
    var group = document.createElement("div");
    group.className = "crush-history-detail-group " + className;
    var h4 = document.createElement("h4");
    h4.textContent = title + " (" + items.length + ")";
    group.appendChild(h4);
    var ul = document.createElement("ul");
    items.forEach(function (item) {
      var li = document.createElement("li");
      li.textContent = formatter(item);
      ul.appendChild(li);
    });
    group.appendChild(ul);
    return group;
  }

  function openDetail(id) {
    fetch("/api/crush-map/history/" + encodeURIComponent(id) + "?cluster_id=" + encodeURIComponent(clusterId), { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        detailTitleEl.textContent = "Chi tiết thay đổi · " + fmtTime(data.created_at);
        detailBodyEl.replaceChildren();
        [["Thêm mới", data.added, "is-added", function (i) { return (i.type || "?") + " " + (i.name || ("#" + i.id)) + " · Weight " + fmtWeight(i.weight); }],
          ["Đã xoá", data.removed, "is-removed", function (i) { return (i.type || "?") + " " + (i.name || ("#" + i.id)) + " · Weight " + fmtWeight(i.weight); }],
          ["Đổi Weight", data.reweighted, "is-reweighted", function (i) { return (i.type || "?") + " " + (i.name || ("#" + i.id)) + ": " + fmtWeight(i.old_weight) + " → " + fmtWeight(i.new_weight); }]
        ].forEach(function (groupData) {
          var group = renderDiffGroup(groupData[0], groupData[1], groupData[3], groupData[2]);
          if (group) detailBodyEl.appendChild(group);
        });
        if (!detailBodyEl.children.length) detailBodyEl.textContent = "Không có chi tiết thay đổi.";
        detailEl.hidden = false;
      })
      .catch(function () { window.alert("Không tải được chi tiết lịch sử CRUSH Map."); });
  }

  function appendCount(parent, count, label, className) {
    if (!count) return;
    var badge = document.createElement("span");
    badge.className = "crush-change-badge " + className;
    badge.textContent = count + " " + label;
    parent.appendChild(badge);
  }

  function renderItems(items) {
    listEl.replaceChildren();
    (items || []).forEach(function (item) {
      var li = document.createElement("li");
      li.className = "crush-history-item";
      var time = document.createElement("time");
      time.className = "crush-history-time";
      time.dateTime = item.created_at;
      time.textContent = fmtTime(item.created_at);
      li.appendChild(time);
      var summary = document.createElement("div");
      summary.className = "crush-history-summary";
      appendCount(summary, item.added_count, "thêm", "is-added");
      appendCount(summary, item.removed_count, "xoá", "is-removed");
      appendCount(summary, item.reweighted_count, "đổi Weight", "is-reweighted");
      if (!summary.children.length) summary.textContent = "Không có thay đổi rõ rệt";
      li.appendChild(summary);
      var action = document.createElement("button");
      action.type = "button";
      action.className = "btn btn-ghost btn-sm crush-history-detail-btn";
      action.textContent = "Xem chi tiết";
      action.setAttribute("aria-label", "Xem chi tiết thay đổi lúc " + fmtTime(item.created_at));
      action.addEventListener("click", function () { openDetail(item.id); });
      li.appendChild(action);
      li.addEventListener("dblclick", function () { openDetail(item.id); });
      listEl.appendChild(li);
    });
  }

  function updatePagination(nextBefore) {
    prevBtn.disabled = requestInFlight || currentPage <= 1;
    nextBtn.disabled = requestInFlight || currentPage >= historyPages || !nextBefore;
    pageEl.textContent = "Trang " + currentPage + "/" + historyPages;
    var first = historyTotal ? ((currentPage - 1) * historyPageSize + 1) : 0;
    var last = Math.min(currentPage * historyPageSize, historyTotal);
    if (countEl) { countEl.textContent = "Hiển thị " + first + "–" + last + " / " + historyTotal + " mục"; countEl.dataset.mobileSummary = first + "–" + last + " / " + historyTotal; }
    if (pagesEl && window.DashboardPagination) window.DashboardPagination.renderPages(pagesEl, currentPage, historyPages, function (targetPage) { if (targetPage <= pageCursors.length) loadPage(targetPage); });
  }

  function loadPage(page) {
    if (requestInFlight || page < 1 || (!pageCursors[page - 1] && page !== 1)) return;
    requestInFlight = true;
    updatePagination(null);
    var before = pageCursors[page - 1];
    var url = "/api/crush-map/history?cluster_id=" + encodeURIComponent(clusterId) + "&limit=" + historyPageSize + (before ? "&before=" + encodeURIComponent(before) : "");
    fetch(url, { credentials: "same-origin", cache: "no-store" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        currentPage = page;
        historyTotal = Number(data.total || 0);
        historyPages = Math.max(1, Math.ceil(historyTotal / historyPageSize));
        renderItems(data.items || []);
        if (data.next_before) pageCursors[page] = data.next_before; else pageCursors.length = page;
        emptyEl.hidden = (data.items || []).length > 0 || page !== 1;
        listEl.hidden = (data.items || []).length === 0;
        updatePagination(data.next_before);
      })
      .catch(function () { console.error("Không tải được lịch sử CRUSH Map"); })
      .finally(function () {
        requestInFlight = false;
        updatePagination(pageCursors[currentPage]);
      });
  }

  prevBtn.addEventListener("click", function () { loadPage(currentPage - 1); });
  nextBtn.addEventListener("click", function () { loadPage(currentPage + 1); });
  purgeBtn.addEventListener("click", function () {
    if (!window.confirm("Xóa toàn bộ lịch sử thay đổi cấu trúc CRUSH của cluster này? Không thể hoàn tác.")) return;
    purgeBtn.disabled = true;
    fetch("/api/crush-map/history/purge?cluster_id=" + encodeURIComponent(clusterId), { method: "POST", credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) return response.json().then(function (body) { throw new Error(body.detail || "Xóa thất bại"); });
        return response.json();
      })
      .then(function () { pageCursors = [null]; currentPage = 1; loadPage(1); })
      .catch(function (error) { window.alert("Không xóa được lịch sử CRUSH Map: " + error.message); })
      .finally(function () { purgeBtn.disabled = false; });
  });
  detailCloseBtn.addEventListener("click", function () { detailEl.hidden = true; });
  detailEl.addEventListener("click", function (event) { if (event.target === detailEl) detailEl.hidden = true; });
  loadPage(1);
})();
