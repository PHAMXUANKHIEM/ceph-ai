(function () {
  var form = document.getElementById("deploy-form");
  var initialStateEl = document.getElementById("deploy-initial-state");
  if (!initialStateEl) return;

  var initialState = JSON.parse(initialStateEl.textContent || "{}");
  var notYetSupported = initialState.not_yet_supported_methods || [];
  var POLL_INTERVAL_MS = 2500;
  var TERMINAL_STATUSES = ["EXECUTED", "FAILED"];
  var STATUS_GLYPH = { pending: "⏸", running: "⏳", done: "✅", failed: "❌" };
  var HOST_KEY_MISMATCH_RE = /Host key for server .* does not match/;

  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = String(value == null ? "" : value);
    return div.innerHTML;
  }
  function pad2(n) { return String(n).padStart(2, "0"); }
  function nowClock() {
    var d = new Date();
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }
  function stepClock(step) {
    return step.status === "running" ? nowClock() : (step.finished_at_display || step.started_at_display || "");
  }

  /* Wizard ------------------------------------------------------------ */
  var currentStep = 1;
  var stepPanels = document.querySelectorAll("[data-step]");
  var stepNav = document.querySelectorAll("[data-step-nav]");
  var prevBtn = document.getElementById("df-prev-step");
  var nextBtn = document.getElementById("df-next-step");
  var submitBtn = document.getElementById("df-submit");
  var footerStep = document.getElementById("df-footer-step");
  var errorEl = document.getElementById("df-error");

  function showStep(step) {
    currentStep = Math.max(1, Math.min(3, step));
    Array.prototype.forEach.call(stepPanels, function (panel) {
      var active = Number(panel.getAttribute("data-step")) === currentStep;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
    Array.prototype.forEach.call(stepNav, function (nav) {
      var number = Number(nav.getAttribute("data-step-nav"));
      nav.classList.toggle("is-active", number === currentStep);
      nav.classList.toggle("is-complete", number < currentStep);
    });
    if (prevBtn) prevBtn.disabled = currentStep === 1;
    if (nextBtn) nextBtn.hidden = currentStep === 3;
    if (submitBtn) submitBtn.hidden = currentStep !== 3;
    if (footerStep) footerStep.textContent = "Bước " + currentStep + " / 3";
  }
  function validateCurrentStep() {
    if (currentStep !== 1 || !form) return true;
    var version = document.getElementById("df-version");
    if (version && !version.checkValidity()) { version.reportValidity(); return false; }
    return true;
  }
  if (nextBtn) nextBtn.addEventListener("click", function () { if (validateCurrentStep()) showStep(currentStep + 1); });
  if (prevBtn) prevBtn.addEventListener("click", function () { showStep(currentStep - 1); });
  Array.prototype.forEach.call(stepNav, function (nav) {
    nav.addEventListener("click", function () {
      var target = Number(nav.getAttribute("data-step-nav"));
      if (target > currentStep && !validateCurrentStep()) return;
      showStep(target);
    });
  });
  showStep(1);

  /* Nodes ------------------------------------------------------------- */
  var nodeCardsEl = document.getElementById("node-rows");
  var addNodeBtn = document.getElementById("df-add-node");
  var nodeCountEl = document.getElementById("df-node-count");
  var nodeNumber = 0;
  function makeRole(role, icon, label) {
    var wrapper = document.createElement("label");
    wrapper.className = "node-role-toggle";
    var input = document.createElement("input"); input.type = "checkbox"; input.className = "node-role"; input.value = role;
    var text = document.createElement("span"); text.innerHTML = '<b>' + escapeHtml(icon) + '</b><strong>' + escapeHtml(label) + '</strong><i></i>';
    wrapper.appendChild(input); wrapper.appendChild(text);
    return wrapper;
  }
  function updateNodeCount() {
    var count = nodeCardsEl ? nodeCardsEl.querySelectorAll(".node-card").length : 0;
    if (nodeCountEl) nodeCountEl.textContent = count + " node" + (count === 1 ? "" : "s");
  }
  function addNodeCard(ip) {
    if (!nodeCardsEl) return;
    nodeNumber += 1;
    var card = document.createElement("article"); card.className = "node-card";
    var header = document.createElement("div"); header.className = "node-card-header"; header.innerHTML = '<span class="node-card-number">NODE ' + String(nodeNumber).padStart(2, "0") + '</span>';
    var remove = document.createElement("button"); remove.type = "button"; remove.className = "node-remove"; remove.title = "Xóa node"; remove.setAttribute("aria-label", "Xóa node"); remove.textContent = "🗑️";
    header.appendChild(remove); card.appendChild(header);
    var ipLabel = document.createElement("label"); ipLabel.className = "node-ip-field"; ipLabel.appendChild(document.createTextNode("IP address"));
    var ipInput = document.createElement("input"); ipInput.type = "text"; ipInput.className = "node-ip"; ipInput.placeholder = "10.20.1.112"; ipInput.value = ip || ""; ipInput.autocomplete = "off";
    ipLabel.appendChild(ipInput); card.appendChild(ipLabel);
    var roles = document.createElement("div"); roles.className = "node-role-list";
    [["mon", "◉", "MON"], ["mgr", "◆", "MGR"], ["osd", "◈", "OSD"]].forEach(function (item) { roles.appendChild(makeRole(item[0], item[1], item[2])); });
    card.appendChild(roles);
    var diskField = document.createElement("label"); diskField.className = "node-disk-field"; diskField.hidden = true;
    var diskLabel = document.createElement("span"); diskLabel.innerHTML = 'OSD Disks <button type="button" class="field-info" title="Nhập một hoặc nhiều thiết bị, phân tách bằng dấu phẩy">ⓘ</button>';
    var diskInput = document.createElement("input"); diskInput.type = "text"; diskInput.className = "node-osd-disk"; diskInput.placeholder = "/dev/vdb, /dev/vdc";
    diskField.appendChild(diskLabel); diskField.appendChild(diskInput); card.appendChild(diskField);
    var osdCheckbox = card.querySelector('input[value="osd"]');
    osdCheckbox.addEventListener("change", function () { diskField.hidden = !osdCheckbox.checked; if (!osdCheckbox.checked) diskInput.value = ""; });
    remove.addEventListener("click", function () { card.remove(); updateNodeCount(); });
    nodeCardsEl.appendChild(card); updateNodeCount();
  }
  if (addNodeBtn) addNodeBtn.addEventListener("click", function () { addNodeCard(); });
  if (nodeCardsEl && !nodeCardsEl.children.length) { addNodeCard(); addNodeCard(); addNodeCard(); }
  function collectNodes() {
    var nodes = [];
    if (!nodeCardsEl) return nodes;
    Array.prototype.forEach.call(nodeCardsEl.querySelectorAll(".node-card"), function (card) {
      var ip = card.querySelector(".node-ip").value.trim(); if (!ip) return;
      var roles = []; Array.prototype.forEach.call(card.querySelectorAll(".node-role:checked"), function (cb) { roles.push(cb.value); });
      var node = { ip: ip, roles: roles };
      if (roles.indexOf("osd") !== -1) { var disks = card.querySelector(".node-osd-disk").value.trim(); node.osd_disks = disks ? disks.split(",").map(function (d) { return d.trim(); }).filter(Boolean) : []; }
      nodes.push(node);
    });
    return nodes;
  }

  /* Version and method ----------------------------------------------- */
  var rpmPathLabel = document.getElementById("df-rpm-path-label");
  var versionInput = document.getElementById("df-version");
  var codenameSelect = document.getElementById("df-codename");
  var versionSelect = document.getElementById("df-version-select");
  var versionsByCodenameEl = document.getElementById("versions-by-codename-data");
  function currentMethod() { var checked = document.querySelector('input[name="method"]:checked'); return checked ? checked.value : "cephadm"; }
  function onMethodChange() {
    var method = currentMethod(); if (rpmPathLabel) rpmPathLabel.hidden = method !== "rpm-local";
    Array.prototype.forEach.call(document.querySelectorAll(".deploy-method-card"), function (card) { card.classList.toggle("is-selected", !!card.querySelector("input:checked")); });
  }
  Array.prototype.forEach.call(document.querySelectorAll('input[name="method"]'), function (radio) { radio.addEventListener("change", onMethodChange); }); onMethodChange();
  if (codenameSelect && versionSelect && versionsByCodenameEl) {
    var versionsByCodename = JSON.parse(versionsByCodenameEl.textContent || "{}");
    codenameSelect.addEventListener("change", function () {
      var versions = versionsByCodename[codenameSelect.value] || []; versionSelect.innerHTML = ""; versionSelect.disabled = !codenameSelect.value || !versions.length;
      if (!versions.length) { var empty = document.createElement("option"); empty.textContent = "— Chọn dòng release trước —"; versionSelect.appendChild(empty); return; }
      for (var i = versions.length - 1; i >= 0; i -= 1) { var option = document.createElement("option"); option.value = versions[i]; option.textContent = versions[i]; versionSelect.appendChild(option); }
      versionInput.value = versions[versions.length - 1];
    });
    versionSelect.addEventListener("change", function () { if (versionSelect.value) versionInput.value = versionSelect.value; });
  }

  /* Submit ------------------------------------------------------------ */
  if (form) form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (currentStep !== 3) { showStep(currentStep + 1); return; }
    if (errorEl) { errorEl.hidden = true; errorEl.textContent = ""; }
    var method = currentMethod();
    if (notYetSupported.indexOf(method) !== -1) { if (errorEl) { errorEl.textContent = "Phương thức này chưa được hỗ trợ tự động — chọn cephadm."; errorEl.hidden = false; } return; }
    var payload = { version: versionInput ? versionInput.value.trim() : "", method: method, rpm_path: document.getElementById("df-rpm-path") ? document.getElementById("df-rpm-path").value.trim() : "", nodes: collectNodes(), public_network: document.getElementById("df-public-network").value.trim(), cluster_network: document.getElementById("df-cluster-network").value.trim(), osd_pool_default_size: parseInt(document.getElementById("df-pool-size").value, 10) || 3, osd_pool_default_min_size: parseInt(document.getElementById("df-pool-min-size").value, 10) || 2 };
    if (submitBtn) { submitBtn.disabled = true; submitBtn.innerHTML = '<span class="deploy-spinner"></span> Đang cài đặt...'; }
    fetch("/deploy-cluster/propose", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).then(function (response) { if (!response.ok) return response.json().then(function (data) { throw new Error(data.detail || "HTTP " + response.status); }); return response.json(); }).then(function () { window.location.reload(); }).catch(function (err) { if (submitBtn) { submitBtn.disabled = false; submitBtn.innerHTML = "<span>▶</span> Bắt đầu cài đặt"; } if (errorEl) { errorEl.textContent = err.message || "Không tạo được đề xuất dựng cụm"; errorEl.hidden = false; } });
  });

  /* Log --------------------------------------------------------------- */
  var logBox = document.getElementById("df-log-box");
  var progressBarFill = document.getElementById("df-progress-bar-fill");
  var progressBar = document.getElementById("df-progress-bar");
  var progressLabel = document.getElementById("df-progress-label");
  var progressStep = document.getElementById("df-progress-step");
  var logTitle = document.getElementById("df-log-title");
  var clearBtn = document.getElementById("df-log-clear");
  var copyBtn = document.getElementById("df-log-copy");
  function renderForgetHostKeyControl(container, host) {
    var row = document.createElement("div"); row.className = "deploy-log-host-action"; row.textContent = "SSH host key của " + host + " đã thay đổi. ";
    if (!initialState.is_admin) { row.appendChild(document.createTextNode("Cần tài khoản admin để xoá key cũ.")); container.appendChild(row); return; }
    var button = document.createElement("button"); button.type = "button"; button.className = "btn btn-sm"; button.textContent = "Xoá SSH host key cũ";
    button.addEventListener("click", function () { button.disabled = true; button.textContent = "Đang xoá..."; fetch("/deploy-cluster/forget-host-key", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ host: host }) }).then(function (response) { return response.json().then(function (data) { return { ok: response.ok, data: data }; }); }).then(function (result) { row.textContent = result.data.message || (result.ok ? "Đã xử lý." : "Có lỗi xảy ra, thử lại."); }).catch(function () { button.disabled = false; button.textContent = "Xoá SSH host key cũ"; }); });
    row.appendChild(button); container.appendChild(row);
  }
  function renderProgress(status, progress) {
    if (!logBox || !progress || !progress.length) return;
    logBox.innerHTML = ""; var runningStep = null; var failedStep = null;
    progress.forEach(function (step) {
      var details = document.createElement("details"); details.className = "deploy-log-section status-" + (step.status || "pending"); if (step.status === "running" || step.status === "failed") details.open = true;
      var summary = document.createElement("summary"); var time = stepClock(step); summary.innerHTML = '<span class="deploy-log-status-icon">' + (STATUS_GLYPH[step.status] || "⏸") + '</span><strong>' + escapeHtml(step.label || step.step) + '</strong><time>' + escapeHtml(time) + '</time><span class="deploy-log-chevron">⌄</span>'; details.appendChild(summary);
      var detail = document.createElement("div"); detail.className = "deploy-log-detail";
      if (step.message) { var message = document.createElement("p"); message.textContent = step.message; detail.appendChild(message); }
      (step.hosts || []).forEach(function (host) { var hostLine = document.createElement("div"); hostLine.className = "deploy-log-host status-" + (host.status || "pending"); hostLine.innerHTML = '<span>' + (STATUS_GLYPH[host.status] || "⏸") + '</span><strong>' + escapeHtml(host.host) + '</strong>' + (host.message ? '<em>' + escapeHtml(host.message) + '</em>' : ''); detail.appendChild(hostLine); });
      if (step.status === "failed" && step.message && HOST_KEY_MISMATCH_RE.test(step.message)) { var failedHost = (step.hosts || []).filter(function (host) { return host.status === "failed"; })[0]; if (failedHost) renderForgetHostKeyControl(detail, failedHost.host); }
      if (!detail.children.length) { var empty = document.createElement("p"); empty.className = "hint"; empty.textContent = step.status === "pending" ? "Chưa bắt đầu" : "Đã hoàn tất"; detail.appendChild(empty); }
      details.appendChild(detail); logBox.appendChild(details); if (step.status === "running") runningStep = step; if (step.status === "failed" && !failedStep) failedStep = step;
    });
    var lastDone = progress.filter(function (s) { return s.status === "done"; }).pop(); var pct = runningStep ? runningStep.pct : (failedStep ? failedStep.pct : lastDone ? lastDone.pct : 0);
    if (progressBarFill) { progressBarFill.style.width = pct + "%"; progressBarFill.classList.toggle("is-active", !!runningStep); }
    if (progressBar) progressBar.setAttribute("aria-valuenow", String(pct));
    if (progressLabel) progressLabel.textContent = pct + "%" + (failedStep ? " — Lỗi ở bước: " + (failedStep.label || failedStep.step) : runningStep ? " — " + (runningStep.label || runningStep.step) : pct === 100 ? " — Hoàn tất" : "");
    if (progressStep) progressStep.textContent = runningStep ? (runningStep.label || runningStep.step) : failedStep ? "Cần xử lý lỗi" : pct === 100 ? "Hoàn tất" : "Sẵn sàng";
    if (logTitle) logTitle.textContent = status === "EXECUTED" ? "Hoàn tất" : status === "FAILED" ? "Thất bại" : status === "APPROVED" ? "Đang cài đặt" : "Nhật ký tiến trình";
  }
  var pollTimer = null;
  function pollOnce() { fetch("/deploy-cluster/progress", { credentials: "same-origin" }).then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); }).then(function (data) { renderProgress(data.status, data.progress); if (data.status && TERMINAL_STATUSES.indexOf(data.status) !== -1) { if (pollTimer) clearInterval(pollTimer); window.location.reload(); } }).catch(function () {}); }
  if (logBox) { renderProgress(initialState.status, initialState.progress); if (initialState.status === "APPROVED") { pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS); pollOnce(); } }
  if (clearBtn && logBox) clearBtn.addEventListener("click", function () { logBox.innerHTML = ""; });
  if (copyBtn && logBox) copyBtn.addEventListener("click", function () { var content = logBox.innerText || logBox.textContent || ""; if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(content); });
  Array.prototype.forEach.call(document.querySelectorAll("[data-open-log]"), function (button) { button.addEventListener("click", function () { var panel = document.getElementById("deploy-log-panel"); if (window.matchMedia && window.matchMedia("(max-width: 900px)").matches) document.body.classList.add("deploy-log-drawer-open"); else if (panel) panel.scrollIntoView({ behavior: "smooth", block: "start" }); }); });
  Array.prototype.forEach.call(document.querySelectorAll("[data-close-log]"), function (button) { button.addEventListener("click", function () { document.body.classList.remove("deploy-log-drawer-open"); }); });
})();
