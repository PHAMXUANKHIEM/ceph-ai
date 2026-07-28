(function () {
  var panel = document.getElementById("volumes-panel");
  if (!panel) {
    return; // /volumes with no pool selected, or not on this page at all
  }

  var pool = panel.dataset.pool;
  var POLL_INTERVAL_MS = 3000;

  var tbody = document.getElementById("volumes-table-body");
  var errorFooter = document.getElementById("volumes-error-footer");
  var errorTimestamp = document.getElementById("volumes-error-timestamp");
  var spinner = document.getElementById("header-spinner");
  var retryBtn = document.getElementById("volumes-retry-btn");

  function pad2(n) { return String(n).padStart(2, "0"); }
  function formatClock(date) {
    return pad2(date.getHours()) + ":" + pad2(date.getMinutes()) + ":" + pad2(date.getSeconds());
  }

  function fmt(v) {
    return typeof v === "number" ? v.toFixed(1) : "—";
  }

  function render(images) {
    tbody.innerHTML = "";
    if (!images.length) {
      var emptyRow = document.createElement("tr");
      emptyRow.className = "empty-row";
      var cell = document.createElement("td");
      cell.colSpan = 5;
      cell.className = "empty-state";
      cell.textContent = "Không có Volume nào đang có I/O trong pool này.";
      emptyRow.appendChild(cell);
      tbody.appendChild(emptyRow);
      return;
    }
    images.forEach(function (img) {
      var row = document.createElement("tr");
      if (img.saturated) row.className = "progress-item-failed";

      var imageCell = document.createElement("td");
      imageCell.innerHTML = "<code>" + img.image + "</code>";
      row.appendChild(imageCell);

      var iopsCell = document.createElement("td");
      iopsCell.textContent = fmt(img.iops);
      row.appendChild(iopsCell);

      var readCell = document.createElement("td");
      readCell.textContent = fmt(img.read_latency_ms) + " ms";
      row.appendChild(readCell);

      var writeCell = document.createElement("td");
      writeCell.textContent = fmt(img.write_latency_ms) + " ms";
      row.appendChild(writeCell);

      var statusCell = document.createElement("td");
      if (img.saturated) {
        statusCell.innerHTML = '<span class="error">⚠ Bão hoà</span>';
      } else {
        statusCell.textContent = "Bình thường";
      }
      row.appendChild(statusCell);

      tbody.appendChild(row);
    });
  }

  function setError(isError, message) {
    if (errorFooter) errorFooter.hidden = !isError;
    if (isError && errorTimestamp) {
      errorTimestamp.textContent = "Lần thử cuối thất bại lúc " + formatClock(new Date()) + " — " + message;
    }
  }

  function poll() {
    if (spinner) spinner.hidden = false;
    fetch("/api/volumes/" + encodeURIComponent(pool) + "/iostat", { credentials: "same-origin" })
      .then(function (response) {
        // require_login raises a 303 to /login on an expired session —
        // fetch's default redirect mode resolves that transparently,
        // landing here as a 200 whose *final* URL is /login.
        if (response.redirected && response.url.indexOf("/login") !== -1) {
          window.location.reload();
          throw new Error("unauthenticated");
        }
        if (!response.ok) {
          return response.json().then(function (body) {
            throw new Error(body.detail || "HTTP " + response.status);
          });
        }
        return response.json();
      })
      .then(function (data) {
        if (spinner) spinner.hidden = true;
        setError(false);
        render(data.images || []);
      })
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
        if (spinner) spinner.hidden = true;
        setError(true, err.message);
      });
  }

  if (retryBtn) retryBtn.addEventListener("click", poll);

  poll();
  setInterval(poll, POLL_INTERVAL_MS);
})();
