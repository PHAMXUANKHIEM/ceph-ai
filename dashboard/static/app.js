(function () {
  // Client-side column sort for any data table on the page (Incident Feed,
  // Audit Trail) — purely a display convenience over the rows the server
  // already rendered, no re-fetch. Skips the single "empty state" row
  // (.empty-row) since there's nothing to sort. Sort state does not
  // survive a reload (e.g. the Incident Feed's websocket-triggered
  // reload below) — the table simply comes back in server order, which
  // matches a normal page refresh anywhere else on the web.
  function makeSortable(table) {
    const thead = table.querySelector("thead");
    const tbody = table.querySelector("tbody");
    if (!thead || !tbody) return;

    const headers = Array.from(thead.querySelectorAll("th"));
    headers.forEach(function (th, columnIndex) {
      th.setAttribute("aria-sort", "none");
      th.addEventListener("click", function () {
        const rows = Array.from(tbody.querySelectorAll("tr")).filter(
          function (row) { return !row.classList.contains("empty-row"); }
        );
        if (rows.length === 0) return;

        const currentlyAscending = th.getAttribute("aria-sort") === "ascending";
        const nextDirection = currentlyAscending ? "descending" : "ascending";
        headers.forEach(function (h) { h.setAttribute("aria-sort", "none"); });
        th.setAttribute("aria-sort", nextDirection);

        const cellText = function (row) {
          const cell = row.children[columnIndex];
          return cell ? cell.textContent.trim().toLowerCase() : "";
        };
        rows.sort(function (a, b) {
          const va = cellText(a);
          const vb = cellText(b);
          if (va < vb) return nextDirection === "ascending" ? -1 : 1;
          if (va > vb) return nextDirection === "ascending" ? 1 : -1;
          return 0;
        });
        rows.forEach(function (row) { tbody.appendChild(row); });
      });
    });
  }

  document.querySelectorAll("table").forEach(makeSortable);
})();

(function () {
  // Shared application shell.  The server-rendered navigation remains the
  // source of truth; this layer only enriches it with a responsive menu,
  // compact product identity and page context so every route gets the same
  // control-plane layout without duplicating presentation markup.
  var topbar = document.querySelector(".topbar");
  var main = document.querySelector("main.page");
  if (!topbar || !main) return;

  document.body.classList.add("app-shell");

  var brand = topbar.querySelector(".brand");
  if (brand && !brand.querySelector(".brand-copy")) {
    var brandText = Array.prototype.slice.call(brand.childNodes).filter(function (node) {
      return node.nodeType === Node.TEXT_NODE && node.textContent.trim();
    });
    brandText.forEach(function (node) { node.remove(); });
    var copy = document.createElement("span");
    copy.className = "brand-copy";
    copy.innerHTML = "<strong>Ceph AI</strong><small>Autonomous operations</small>";
    brand.appendChild(copy);
  }

  var menuButton = document.createElement("button");
  menuButton.type = "button";
  menuButton.className = "shell-menu-toggle";
  menuButton.setAttribute("aria-label", "Mở menu điều hướng");
  menuButton.setAttribute("aria-expanded", "false");
  menuButton.innerHTML = "<span></span><span></span><span></span>";
  topbar.querySelector(".topbar-inner").insertBefore(menuButton, topbar.querySelector(".main-nav"));

  function setMenu(open) {
    document.body.classList.toggle("nav-open", open);
    menuButton.setAttribute("aria-expanded", open ? "true" : "false");
    menuButton.setAttribute("aria-label", open ? "Đóng menu điều hướng" : "Mở menu điều hướng");
  }
  menuButton.addEventListener("click", function () {
    setMenu(!document.body.classList.contains("nav-open"));
  });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") setMenu(false);
  });
  window.addEventListener("resize", function () {
    if (window.innerWidth >= 1024) setMenu(false);
  });

  var title = document.title.split("—")[0].trim()
    .replace("Ceph AIOps Dashboard", "Operations overview");
  var heading = document.createElement("header");
  heading.className = "page-heading";
  heading.innerHTML =
    '<div><span class="page-eyebrow">CEPH AI · CONTROL PLANE</span>' +
    '<h1></h1><p>Giám sát, phân tích và vận hành hạ tầng Ceph trong một không gian thống nhất.</p></div>' +
    '<span class="page-live"><i></i> SYSTEM LIVE</span>';
  heading.querySelector("h1").textContent = title;
  main.insertBefore(heading, main.firstChild);

  var iconByPath = {
    "/": "⌁", "/nodes": "◫", "/volumes": "◉", "/settings": "⚙",
    "/telegram-alerts": "↗", "/users": "♙", "/clusters": "⬡",
    "/crush-map": "⌘", "/deploy-cluster": "+", "/delete-cluster": "−",
    "/convert-cluster": "⇄", "/upgrade": "↑", "/patch": "◇",
    "/backups": "□", "/restore-cluster": "↶", "/bucket-access-log": "≡", "/pgs": "∷"
  };

  var mainNav = topbar.querySelector(".main-nav");
  if (mainNav) {
    var linksByPath = {};
    mainNav.querySelectorAll("a").forEach(function (link) {
      linksByPath[new URL(link.href, window.location.origin).pathname] = link;
    });
    if (!linksByPath["/pgs"]) {
      var pgLink = document.createElement("a");
      pgLink.href = "/pgs";
      pgLink.className = window.location.pathname === "/pgs" ? "nav-link active" : "nav-link";
      pgLink.textContent = "PGs";
      linksByPath["/pgs"] = pgLink;
    }

    // Information architecture for Ceph operators. Keep permission-aware
    // links from the server (admin-only destinations may not exist), then
    // regroup only those that were actually rendered.
    var navGroups = [
      { label: "Monitoring & Metrics", paths: ["/", "/nodes", "/crush-map"] },
      { label: "Pool", paths: ["/volumes", "/pgs"] },
      { label: "Object Storage", paths: ["/bucket-access-log"] },
      { label: "Cluster Lifecycle Management", paths: ["/deploy-cluster", "/delete-cluster", "/upgrade", "/patch", "/convert-cluster"] },
      { label: "Backup", paths: ["/backups", "/restore-cluster"] },
      { label: "Users & Notifications", paths: ["/telegram-alerts", "/users"] },
      { label: "System Administration", paths: ["/settings", "/clusters"] }
    ];
    mainNav.innerHTML = "";
    navGroups.forEach(function (group) {
      var available = group.paths.filter(function (path) { return linksByPath[path]; });
      if (!available.length) return;
      var section = document.createElement("section");
      section.className = "nav-section";
      var groupKey = group.label.toLowerCase().replace(/[^a-z0-9]+/g, "-");
      var toggle = document.createElement("button");
      toggle.type = "button";
      toggle.className = "nav-section-toggle";
      toggle.innerHTML = '<span class="nav-section-label"></span><span class="nav-section-chevron" aria-hidden="true">⌄</span>';
      toggle.querySelector(".nav-section-label").textContent = group.label;
      var items = document.createElement("div");
      items.className = "nav-section-items";
      items.id = "nav-group-" + groupKey;
      toggle.setAttribute("aria-controls", items.id);
      section.appendChild(toggle);
      section.appendChild(items);
      available.forEach(function (path) {
        var link = linksByPath[path];
        if (link.classList.contains("nav-dropdown-item-active")) link.classList.add("active");
        link.classList.remove("nav-dropdown-item", "nav-dropdown-item-active");
        link.classList.add("nav-link");
        if (path === "/volumes") {
          Array.prototype.slice.call(link.childNodes).forEach(function (node) {
            if (node.nodeType === Node.TEXT_NODE && node.textContent.trim()) node.textContent = "Pool";
          });
        }
        items.appendChild(link);
      });

      var storageKey = "cephAiNavGroup:" + groupKey;
      var hasActiveLink = !!items.querySelector(".active");
      var expanded = hasActiveLink;
      if (!hasActiveLink) {
        try {
          var saved = localStorage.getItem(storageKey);
          if (saved !== null) expanded = saved === "1";
        } catch (e) {
          // Storage may be disabled; active group remains the useful default.
        }
      }
      function setExpanded(open) {
        items.hidden = !open;
        section.classList.toggle("is-open", open);
        toggle.setAttribute("aria-expanded", open ? "true" : "false");
      }
      setExpanded(expanded);
      toggle.addEventListener("click", function () {
        var open = items.hidden;
        setExpanded(open);
        try { localStorage.setItem(storageKey, open ? "1" : "0"); } catch (e) { /* optional preference */ }
      });
      mainNav.appendChild(section);
    });
  }

  topbar.querySelectorAll(".main-nav a").forEach(function (link) {
    if (link.querySelector(".nav-icon")) return;
    var path = new URL(link.href, window.location.origin).pathname;
    var icon = document.createElement("span");
    icon.className = "nav-icon";
    icon.setAttribute("aria-hidden", "true");
    icon.textContent = iconByPath[path] || "·";
    link.insertBefore(icon, link.firstChild);
  });
})();

(function () {
  if (!document.getElementById("incident-feed")) {
    return; // not on the dashboard page (e.g. login page)
  }

  const RECONNECT_DELAY_MS = 2000;

  function connect() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + window.location.host + "/ws/incidents");
    ws.onmessage = function () {
      // Update live widgets in place. A full reload used to reset scroll,
      // tabs, expanded cards and chat state whenever Watcher changed data.
      window.dispatchEvent(new CustomEvent("ceph-dashboard-refresh"));
    };
    // A closed/errored socket (server restart, idle timeout, network blip)
    // must not leave the page silently stale — reconnect after a short delay.
    ws.onclose = function () {
      setTimeout(connect, RECONNECT_DELAY_MS);
    };
    ws.onerror = function () {
      ws.close();
    };
  }

  connect();
})();

(function () {
  // Generic sidebar + single-panel tab switching (2026-07-24), shared by
  // every page using the .tabbed-sidebar/.tabbed-nav-item/.tabbed-panel
  // classes (dashboard/static/style.css) — settings.html has its own
  // equivalent in settings.js under the older .settings-* names, since it
  // loads settings.js instead of this file. The server picks which panel
  // starts visible (each route's own "active tab" logic, so a form's
  // error/success message after a POST always lands on the right panel,
  // not hidden behind whichever one happened to be first) — this only
  // handles CLICKING a different item without a page reload.
  var tabNavItems = Array.prototype.slice.call(document.querySelectorAll(".tabbed-nav-item"));
  var tabPanels = Array.prototype.slice.call(document.querySelectorAll(".tabbed-panel"));
  if (tabNavItems.length && tabPanels.length) {
    tabNavItems.forEach(function (item) {
      item.addEventListener("click", function () {
        var section = item.getAttribute("data-section");
        tabNavItems.forEach(function (other) {
          other.classList.toggle("active", other === item);
        });
        tabPanels.forEach(function (panel) {
          panel.hidden = panel.getAttribute("data-panel") !== section;
        });
      });
    });
  }
})();

(function () {
  // "Tự động mở tab này khi có Risky Action mới" checkbox (Dashboard home
  // page only, 2026-07-28). dashboard/routes/incidents.py::index always
  // defaults active_tab to "pending" on a plain GET / — combined with the
  // WebSocket handler above, which does a full window.location.reload() on
  // EVERY Incident/Action change system-wide (not just ones the operator
  // is looking at), this meant the page kept yanking an operator back to
  // the "Chờ duyệt" tab mid-task any time they'd manually switched to
  // Incident Feed/Audit Trail to look at something unrelated. Turning this
  // off remembers whichever tab was last clicked and restores THAT instead
  // of the server's "pending" default on the next reload — same
  // localStorage-persistence posture as the collapse toggle below (this
  // page's own reload defeats any purely in-memory state).
  var pendingTabBtn = document.querySelector('.tabbed-nav-item[data-section="pending"]');
  var autoSwitchCheckbox = document.getElementById("pending-tab-autoswitch");
  if (pendingTabBtn && autoSwitchCheckbox) {
    var AUTO_SWITCH_KEY = "pendingTabAutoSwitch";
    var LAST_TAB_KEY = "dashboardLastActiveTab";
    var tabNavItems = Array.prototype.slice.call(document.querySelectorAll(".tabbed-nav-item"));
    var tabPanels = Array.prototype.slice.call(document.querySelectorAll(".tabbed-panel"));

    var activateTab = function (section) {
      tabNavItems.forEach(function (item) {
        item.classList.toggle("active", item.getAttribute("data-section") === section);
      });
      tabPanels.forEach(function (panel) {
        panel.hidden = panel.getAttribute("data-panel") !== section;
      });
    };

    var autoSwitch = true;
    try {
      autoSwitch = localStorage.getItem(AUTO_SWITCH_KEY) !== "0";
    } catch (e) {
      // localStorage unavailable — default to current (always-on) behavior
    }
    autoSwitchCheckbox.checked = autoSwitch;

    if (!autoSwitch) {
      var lastTab = null;
      try {
        lastTab = localStorage.getItem(LAST_TAB_KEY);
      } catch (e) {
        // ignore — falls back to whichever tab the server rendered active
      }
      if (lastTab) {
        activateTab(lastTab);
      }
    }

    tabNavItems.forEach(function (item) {
      item.addEventListener("click", function () {
        try {
          localStorage.setItem(LAST_TAB_KEY, item.getAttribute("data-section"));
        } catch (e) {
          // ignore — just won't be restored after the next reload
        }
      });
    });

    autoSwitchCheckbox.addEventListener("change", function () {
      try {
        localStorage.setItem(AUTO_SWITCH_KEY, autoSwitchCheckbox.checked ? "1" : "0");
      } catch (e) {
        // ignore — preference just won't survive a reload
      }
    });
  }
})();

(function () {
  // "Chờ duyệt — Risky Action" card (Dashboard home page only): pure
  // in localStorage since this page's WebSocket auto-reloads on every
  // Incident/Action change (dashboard/ws.py) — without persistence the
  // card would silently re-expand on the very next reload.
  var STORAGE_KEY = "pendingActionsCollapsed";
  var toggleBtn = document.getElementById("pending-actions-toggle");
  var bodyEl = document.getElementById("pending-actions-body");
  if (!toggleBtn || !bodyEl) {
    return; // not on the Dashboard home page
  }

  function setCollapsed(collapsed) {
    bodyEl.hidden = collapsed;
    toggleBtn.innerHTML = collapsed ? "&#43;" : "&#8722;";
    var label = collapsed ? "Hiện danh sách" : "Ẩn danh sách";
    toggleBtn.setAttribute("aria-label", label);
    toggleBtn.title = label;
    try {
      localStorage.setItem(STORAGE_KEY, collapsed ? "1" : "0");
    } catch (e) {
      // localStorage unavailable (private mode, quota) — state just won't
      // persist across reloads, not worth failing the toggle over.
    }
  }

  toggleBtn.addEventListener("click", function () {
    setCollapsed(!bodyEl.hidden);
  });

  var storedCollapsed = false;
  try {
    storedCollapsed = localStorage.getItem(STORAGE_KEY) === "1";
  } catch (e) {
    // ignore — default expanded
  }
  setCollapsed(storedCollapsed);
})();

(function () {
  // Top nav "Cluster" dropdown — click to open/close (not hover, unreliable
  // on touch), closes on an outside click or Escape. Loaded on every page
  // that shows the top nav (added to backups.html/settings.html, which
  // don't otherwise load app.js, specifically for this).
  var dropdowns = Array.prototype.slice.call(document.querySelectorAll(".nav-dropdown"));
  if (!dropdowns.length) return;

  function closeAll() {
    dropdowns.forEach(function (d) { d.classList.remove("is-open"); });
  }

  dropdowns.forEach(function (dropdown) {
    var toggle = dropdown.querySelector(".nav-dropdown-toggle");
    if (!toggle) return;
    toggle.addEventListener("click", function (event) {
      event.stopPropagation();
      var wasOpen = dropdown.classList.contains("is-open");
      closeAll();
      if (!wasOpen) dropdown.classList.add("is-open");
    });
  });

  document.addEventListener("click", closeAll);
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") closeAll();
  });
})();
