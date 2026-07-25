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
  if (!document.getElementById("incident-feed")) {
    return; // not on the dashboard page (e.g. login page)
  }

  const RECONNECT_DELAY_MS = 2000;

  function connect() {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(proto + "//" + window.location.host + "/ws/incidents");
    ws.onmessage = function () {
      window.location.reload();
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
