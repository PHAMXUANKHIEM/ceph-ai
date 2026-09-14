(function () {
  // Epic 12, Story 12.3 (F2) — CRUSH tree: lightweight fetch polling that
  // rebuilds/updates the DOM (AD-29), deliberately NOT the app.js WebSocket
  // + window.location.reload() mechanism (that would wipe every expand/
  // collapse the admin just clicked on every new Snapshot). First tree/graph
  // UI in this app — no canvas chart precedent to reuse for the tree itself,
  // only the fetch/interval shape (mirrors backups.js's poll()).

  // CRUSH/distribution data is collected on a much slower Watcher cadence;
  // polling every 5 seconds only rebuilt thousands of DOM nodes repeatedly.
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

  if (!treeEl) {
    return; // not on the CRUSH Map page
  }

  function loadCollapsed() {
    try {
      var raw = localStorage.getItem(COLLAPSED_STORAGE_KEY);
      return raw ? new Set(JSON.parse(raw)) : new Set();
    } catch (e) {
      return new Set(); // private mode/quota — just won't persist
    }
  }

  function saveCollapsed(collapsedSet) {
    try {
      localStorage.setItem(COLLAPSED_STORAGE_KEY, JSON.stringify(Array.from(collapsedSet)));
    } catch (e) {
      // ignore — state just won't survive a reload
    }
  }

  function fmtWeight(w) {
    return typeof w === "number" ? (w / CRUSH_WEIGHT_SCALE).toFixed(3) : "—";
  }

  function buildNodeEl(node, collapsedSet) {
    var wrap = document.createElement("div");
    wrap.className = "crush-node";

    var row = document.createElement("div");
    row.className = "crush-node-row";

    var hasChildren = !!(node.children && node.children.length);
    var nodeKey = String(node.id);
    var isCollapsed = hasChildren && collapsedSet.has(nodeKey);

    var toggleBtn = document.createElement("button");
    toggleBtn.type = "button";
    toggleBtn.className = "crush-node-toggle";
    toggleBtn.textContent = hasChildren ? (isCollapsed ? "▶" : "▼") : "";
    toggleBtn.disabled = !hasChildren;
    row.appendChild(toggleBtn);

    var typeEl = document.createElement("span");
    typeEl.className = "crush-node-type";
    typeEl.textContent = node.type || "?";
    row.appendChild(typeEl);

    var nameEl = document.createElement("span");
    nameEl.className = "crush-node-name";
    nameEl.textContent = node.name || ("#" + node.id);
    row.appendChild(nameEl);

    var weightEl = document.createElement("span");
    weightEl.className = "crush-node-weight";
    weightEl.textContent = "W=" + fmtWeight(node.weight);
    row.appendChild(weightEl);

    if (node.has_distribution_data) {
      var usageEl = document.createElement("span");
      usageEl.className = "crush-node-usage" + (node.partial_distribution_data ? " is-partial" : "");
      var pct = (typeof node.bytes_total === "number" && node.bytes_total > 0)
        ? ((node.bytes_used / node.bytes_total) * 100).toFixed(1) + "%"
        : "—";
      var pgsText = typeof node.pgs === "number" ? node.pgs + " PG" : "— PG";
      usageEl.textContent = pct + " · " + pgsText;
      row.appendChild(usageEl);
    } else {
      var noDataEl = document.createElement("span");
      noDataEl.className = "crush-node-nodata";
      noDataEl.textContent = "chưa có dữ liệu";
      row.appendChild(noDataEl);
    }

    if (node.recent_change) {
      var badge = document.createElement("span");
      var kind = node.recent_change.kind;
      badge.className = "crush-node-badge " + (kind === "added" ? "is-added" : "is-reweighted");
      var changeDesc = kind === "added"
        ? "Mới thêm"
        : "Đổi Weight " + fmtWeight(node.recent_change.old_weight) + " → " + fmtWeight(node.recent_change.new_weight);
      var changedAt = node.recent_change.changed_at
        ? " (" + new Date(node.recent_change.changed_at).toLocaleString("vi-VN") + ")"
        : "";
      badge.textContent = changeDesc + changedAt;
      row.appendChild(badge);
    }

    wrap.appendChild(row);

    if (hasChildren) {
      var childrenEl = document.createElement("div");
      childrenEl.className = "crush-node-children";
      childrenEl.hidden = isCollapsed;
      node.children.forEach(function (child) {
        childrenEl.appendChild(buildNodeEl(child, collapsedSet));
      });
      wrap.appendChild(childrenEl);

      toggleBtn.addEventListener("click", function () {
        var willCollapse = !childrenEl.hidden;
        childrenEl.hidden = willCollapse;
        toggleBtn.textContent = willCollapse ? "▶" : "▼";
        if (willCollapse) {
          collapsedSet.add(nodeKey);
        } else {
          collapsedSet.delete(nodeKey);
        }
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

  function renderRules(rules) {
    if (!rulesEl || !rulesEmptyEl) return;
    rulesEl.innerHTML = "";
    rulesEmptyEl.hidden = !!(rules && rules.length);
    rulesEl.hidden = !(rules && rules.length);
    (rules || []).forEach(function (rule) {
      var card = document.createElement("article");
      card.className = "crush-rule";
      var header = document.createElement("div");
      header.className = "crush-rule-header";
      var name = document.createElement("strong");
      name.textContent = rule.rule_name || ("Rule #" + rule.rule_id);
      header.appendChild(name);
      var meta = document.createElement("span");
      meta.className = "crush-rule-meta";
      meta.textContent = "ID " + (rule.rule_id == null ? "—" : rule.rule_id) + " · " + (rule.type || "—")
        + " · size " + (rule.min_size == null ? "—" : rule.min_size) + "–" + (rule.max_size == null ? "—" : rule.max_size);
      header.appendChild(meta);
      card.appendChild(header);
      var steps = document.createElement("ol");
      steps.className = "crush-rule-steps";
      (rule.steps || []).forEach(function (step) {
        var parts = [step.op || "?"];
        if (step.item_name) parts.push(step.item_name);
        else if (typeof step.item === "number") parts.push(String(step.item));
        if (typeof step.num === "number") parts.push("num=" + step.num);
        if (step.type) parts.push("type=" + step.type);
        var item = document.createElement("li");
        item.textContent = parts.join(" · ");
        steps.appendChild(item);
      });
      card.appendChild(steps);
      rulesEl.appendChild(card);
    });
  }

  function renderTree(data) {
    hideAllStates();

    if (data.state === "no_snapshot_yet") {
      renderRules([]);
      noSnapshotEl.hidden = false;
      return;
    }

    renderRules(data.rules || []);

    metaEl.hidden = false;
    metaEl.textContent = "Snapshot lúc " + new Date(data.created_at).toLocaleString("vi-VN");

    if (data.state === "empty_cluster") {
      emptyClusterEl.hidden = false;
      return;
    }

    // state === "ok" — re-apply the collapse state fresh every poll tick so
    // a re-render never resets what the admin already had open (AD-29).
    var collapsedSet = loadCollapsed();
    treeEl.innerHTML = "";
    var fragment = document.createDocumentFragment();
    (data.roots || []).forEach(function (root) {
      fragment.appendChild(buildNodeEl(root, collapsedSet));
    });
    // One DOM insertion avoids layout work after every individual root.
    treeEl.appendChild(fragment);
    treeEl.hidden = false;
  }

  var lastPayload = null;
  var pollTimer = null;
  var requestInFlight = false;

  function schedulePoll() {
    window.clearTimeout(pollTimer);
    pollTimer = window.setTimeout(poll, POLL_INTERVAL_MS);
  }

  function poll() {
    // Background tabs do no useful visual work. A visibilitychange listener
    // below refreshes immediately when the operator comes back.
    if (document.hidden || requestInFlight) {
      schedulePoll();
      return;
    }
    requestInFlight = true;
    fetch("/api/crush-map/tree", { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        errorEl.hidden = true;
        var payload = JSON.stringify(data);
        // Building a large nested tree is the expensive part. Keep the
        // current DOM untouched when neither structure nor usage changed.
        if (payload !== lastPayload) {
          renderTree(data);
          lastPayload = payload;
        }
      })
      .catch(function () {
        // Keep the last good tree visible during a transient failure. Hiding
        // and rebuilding it caused a noticeable flash and extra layout work.
        errorEl.hidden = false;
      })
      .finally(function () {
        requestInFlight = false;
        schedulePoll();
      });
  }

  poll();
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && !requestInFlight) {
      window.clearTimeout(pollTimer);
      poll();
    }
  });
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
  // Change-history list + detail panel (FR-5/FR62) — independent of the
  // live tree poll above; loaded once, "Xem thêm" paginates via the
  // `before` cursor the API hands back.

  var listEl = document.getElementById("crush-history-list");
  var emptyEl = document.getElementById("crush-history-empty");
  var pageEl = document.getElementById("crush-history-page");
  var prevBtn = document.getElementById("crush-history-prev");
  var nextBtn = document.getElementById("crush-history-next");
  var purgeBtn = document.getElementById("crush-history-purge");
  var detailEl = document.getElementById("crush-history-detail");
  var detailTitleEl = document.getElementById("crush-history-detail-title");
  var detailBodyEl = document.getElementById("crush-history-detail-body");
  var detailCloseBtn = document.getElementById("crush-history-detail-close");

  if (!listEl) {
    return; // not on the CRUSH Map page
  }

  var CRUSH_WEIGHT_SCALE = 65536;
  var currentPage = 1;
  var pageCursors = [null];
  var requestInFlight = false;

  function fmtTime(iso) {
    return new Date(iso).toLocaleString("vi-VN");
  }

  function fmtWeight(w) {
    return typeof w === "number" ? (w / CRUSH_WEIGHT_SCALE).toFixed(3) : "—";
  }

  function openDetail(id) {
    fetch("/api/crush-map/history/" + encodeURIComponent(id), { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        detailTitleEl.textContent = fmtTime(data.created_at);
        detailBodyEl.innerHTML = "";

        var groups = [
          renderDiffGroup("Thêm mới", data.added, function (i) {
            return (i.type || "?") + " " + (i.name || ("#" + i.id)) + " (Weight " + fmtWeight(i.weight) + ")";
          }),
          renderDiffGroup("Đã xoá", data.removed, function (i) {
            return (i.type || "?") + " " + (i.name || ("#" + i.id)) + " (Weight " + fmtWeight(i.weight) + ")";
          }),
          renderDiffGroup("Đổi Weight", data.reweighted, function (i) {
            return (i.type || "?") + " " + (i.name || ("#" + i.id)) + ": " + fmtWeight(i.old_weight) + " → " + fmtWeight(i.new_weight);
          }),
        ];

        var anyGroup = false;
        groups.forEach(function (group) {
          if (group) {
            detailBodyEl.appendChild(group);
            anyGroup = true;
          }
        });
        if (!anyGroup) {
          detailBodyEl.textContent = "Không có chi tiết thay đổi.";
        }

        detailEl.hidden = false;
      })
      .catch(function () {
        // ignore — leave detail panel closed rather than show a broken one
      });
  }

  function renderDiffGroup(title, items, formatter) {
    if (!items || !items.length) return null;
    var group = document.createElement("div");
    group.className = "crush-history-detail-group";
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

  function renderItems(items, append) {
    if (!append) {
      listEl.innerHTML = "";
    }
    items.forEach(function (item) {
      var li = document.createElement("li");
      li.className = "crush-history-item";

      var timeEl = document.createElement("span");
      timeEl.className = "crush-history-time";
      timeEl.textContent = fmtTime(item.created_at);
      li.appendChild(timeEl);

      var summaryEl = document.createElement("span");
      summaryEl.className = "crush-history-summary";
      var parts = [];
      if (item.added_count) parts.push(item.added_count + " thêm");
      if (item.removed_count) parts.push(item.removed_count + " xoá");
      if (item.reweighted_count) parts.push(item.reweighted_count + " đổi Weight");
      summaryEl.textContent = parts.length ? parts.join(", ") : "Không có thay đổi rõ rệt";
      li.appendChild(summaryEl);

      li.addEventListener("click", function () {
        openDetail(item.id);
      });

      listEl.appendChild(li);
    });
  }

  function updatePagination(nextBefore) {
    prevBtn.disabled = requestInFlight || currentPage <= 1;
    nextBtn.disabled = requestInFlight || !nextBefore;
    pageEl.textContent = "Trang " + currentPage;
  }

  function loadPage(page) {
    if (requestInFlight || page < 1 || (!pageCursors[page - 1] && page !== 1)) return;
    requestInFlight = true;
    updatePagination(null);
    var before = pageCursors[page - 1];
    var url = "/api/crush-map/history?limit=10" + (before ? "&before=" + encodeURIComponent(before) : "");
    fetch(url, { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        currentPage = page;
        renderItems(data.items, false);
        if (data.next_before) pageCursors[page] = data.next_before;
        else pageCursors.length = page;
        emptyEl.hidden = data.items.length > 0 || page !== 1;
        listEl.hidden = data.items.length === 0;
        updatePagination(data.next_before);
      })
      .catch(function () {
        console.error("Không tải được lịch sử CRUSH Map");
        updatePagination(pageCursors[page]);
      })
      .finally(function () {
        requestInFlight = false;
        updatePagination(pageCursors[currentPage]);
      });
  }

  prevBtn.addEventListener("click", function () {
    loadPage(currentPage - 1);
  });

  nextBtn.addEventListener("click", function () {
    loadPage(currentPage + 1);
  });

  purgeBtn.addEventListener("click", function () {
    if (!window.confirm("Xóa toàn bộ lịch sử thay đổi cấu trúc CRUSH của cluster này? Không thể hoàn tác.")) return;
    purgeBtn.disabled = true;
    fetch("/api/crush-map/history/purge", {method: "POST", credentials: "same-origin"})
      .then(function (response) {
        if (!response.ok) return response.json().then(function (body) { throw new Error(body.detail || "Xóa thất bại"); });
        return response.json();
      })
      .then(function (data) {
        pageCursors = [null];
        currentPage = 1;
        loadPage(1);
      })
      .catch(function (error) {
        window.alert("Không xóa được lịch sử CRUSH Map: " + error.message);
      })
      .finally(function () {
        purgeBtn.disabled = false;
      });
  });

  detailCloseBtn.addEventListener("click", function () {
    detailEl.hidden = true;
  });

  loadPage(1);
})();
