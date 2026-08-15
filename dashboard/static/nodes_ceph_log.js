(function () {
  var panel = document.getElementById("ceph-log-panel");
  if (!panel) return;

  var host = panel.dataset.host;
  var body = document.getElementById("ceph-log-body");
  var service = document.getElementById("ceph-log-service");
  var filter = document.getElementById("ceph-log-filter");
  var clear = document.getElementById("ceph-log-filter-clear");
  var liveButton = document.getElementById("ceph-log-live-toggle");
  var status = document.getElementById("ceph-log-status");
  var statusText = document.getElementById("ceph-log-status-text");
  var live = true, timer = null, debounce = null, inFlight = false;

  function setStatus(state, text) {
    status.classList.toggle("is-loading", state === "loading");
    status.classList.toggle("is-stale", state === "error");
    statusText.textContent = text;
  }
  function render(lines) {
    while (body.firstChild) body.removeChild(body.firstChild);
    if (!lines.length) {
      var empty = document.createElement("span");
      empty.className = "rgw-log-empty";
      empty.textContent = filter.value.trim() ? "Không có dòng nào khớp bộ lọc." : "Log trống.";
      body.appendChild(empty);
      return;
    }
    lines.forEach(function (text) {
      var line = document.createElement("div");
      line.className = "rgw-log-line";
      line.textContent = text;
      body.appendChild(line);
    });
    body.scrollTop = body.scrollHeight;
  }
  function load() {
    if (inFlight) return;
    inFlight = true;
    setStatus("loading", "Đang tải " + service.value.toUpperCase() + "...");
    var url = "/api/nodes/" + encodeURIComponent(host) + "/ceph-log?service=" +
      encodeURIComponent(service.value) + "&filter=" + encodeURIComponent(filter.value.trim());
    fetch(url, {credentials: "same-origin"})
      .then(function (response) {
        if (response.redirected && response.url.indexOf("/login") !== -1) {
          window.location.reload(); throw new Error("unauthenticated");
        }
        if (!response.ok) return response.json().then(function (data) {
          throw new Error(data.detail || "HTTP " + response.status);
        });
        return response.json();
      })
      .then(function (data) { render(data.lines || []); setStatus("live", "Đã cập nhật"); })
      .catch(function (error) { if (error.message !== "unauthenticated") setStatus("error", "Lỗi: " + error.message); })
      .finally(function () { inFlight = false; });
  }
  function schedule() {
    if (timer) clearTimeout(timer);
    if (live) timer = setTimeout(function () { load(); schedule(); }, 3000);
  }
  liveButton.addEventListener("click", function () {
    live = !live;
    liveButton.textContent = live ? "Tạm dừng" : "Theo dõi trực tiếp";
    if (live) { load(); schedule(); } else { clearTimeout(timer); setStatus("paused", "Đã tạm dừng"); }
  });
  service.addEventListener("change", load);
  filter.addEventListener("input", function () { clearTimeout(debounce); debounce = setTimeout(load, 400); });
  clear.addEventListener("click", function () { filter.value = ""; load(); filter.focus(); });
  liveButton.textContent = "Tạm dừng";
  load(); schedule();
})();
