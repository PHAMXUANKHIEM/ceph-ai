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
