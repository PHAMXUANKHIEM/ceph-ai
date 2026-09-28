(function () {
  var form = document.getElementById("restore-form");
  var initialStateEl = document.getElementById("restore-initial-state");
  if (!initialStateEl) return;

  var initialState = JSON.parse(initialStateEl.textContent || "{}");
  var POLL_INTERVAL_MS = 2500;
  var TERMINAL_STATUSES = ["EXECUTED", "FAILED"];
  var STATUS_GLYPH = { pending: "⏳", running: "🔄", done: "✅", failed: "❌" };

  function pad2(n) { return String(n).padStart(2, "0"); }
  function nowClock() { var d = new Date(); return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds()); }
  function escapeHtml(value) { var div = document.createElement("div"); div.textContent = String(value == null ? "" : value); return div.innerHTML; }

  // --- Two-step wizard -------------------------------------------------
  var currentStep = 1;
  var stepPanels = document.querySelectorAll(".restore-dr-step-panel");
  var stepButtons = document.querySelectorAll("[data-step-nav]");
  var prevStepBtn = document.getElementById("rf-prev-step");
  var nextStepBtn = document.getElementById("rf-next-step");
  var submitBtn = document.getElementById("rf-submit");
  var footerStep = document.getElementById("rf-footer-step");

  function showStep(step) {
    currentStep = step;
    Array.prototype.forEach.call(stepPanels, function (panel) {
      var active = Number(panel.getAttribute("data-step")) === step;
      panel.hidden = !active;
      panel.classList.toggle("is-active", active);
    });
    Array.prototype.forEach.call(stepButtons, function (button) {
      var buttonStep = Number(button.getAttribute("data-step-nav"));
      button.classList.toggle("is-active", buttonStep === step);
      button.classList.toggle("is-complete", buttonStep < step);
      button.setAttribute("aria-current", buttonStep === step ? "step" : "false");
    });
    if (prevStepBtn) prevStepBtn.disabled = step === 1;
    if (nextStepBtn) nextStepBtn.hidden = step === 2;
    if (submitBtn) submitBtn.hidden = step !== 2;
    if (footerStep) footerStep.textContent = "Bước " + step + " / 2";
  }

  function canLeaveFirstStep() {
    var version = document.getElementById("rf-version");
    if (version && !version.value.trim()) { version.reportValidity(); return false; }
    return true;
  }
  Array.prototype.forEach.call(stepButtons, function (button) {
    button.addEventListener("click", function () {
      var target = Number(button.getAttribute("data-step-nav"));
      if (target === 2 && !canLeaveFirstStep()) return;
      showStep(target);
    });
  });
  if (nextStepBtn) nextStepBtn.addEventListener("click", function () { if (canLeaveFirstStep()) showStep(2); });
  if (prevStepBtn) prevStepBtn.addEventListener("click", function () { showStep(1); });
  showStep(1);

  // --- Node cards ------------------------------------------------------
  var nodeRowsEl = document.getElementById("node-rows");
  var addNodeBtn = document.getElementById("rf-add-node");
  var nodeCountEl = document.getElementById("rf-node-count");

  function updateNodeCount() {
    if (nodeCountEl && nodeRowsEl) {
      var count = nodeRowsEl.querySelectorAll(".restore-dr-node-card").length;
      nodeCountEl.textContent = count + (count === 1 ? " node" : " node");
    }
  }

  function addNodeCard(ip) {
    if (!nodeRowsEl) return;
    var card = document.createElement("article");
    card.className = "restore-dr-node-card";
    card.innerHTML =
      '<div class="restore-dr-node-card-header"><span class="restore-dr-node-number"></span><button type="button" class="restore-dr-remove-node" aria-label="Xóa node">×</button></div>' +
      '<label class="restore-dr-ip-field">IP node <span class="required-mark" aria-hidden="true">*</span><input type="text" class="node-ip" placeholder="10.20.1.112" required></label>' +
      '<div class="restore-dr-role-list" aria-label="Vai trò node">' +
        '<label class="restore-dr-role-toggle"><input type="checkbox" class="node-role" value="mon"><span><b>◉</b> MON</span></label>' +
        '<label class="restore-dr-role-toggle"><input type="checkbox" class="node-role" value="mgr"><span><b>◆</b> MGR</span></label>' +
        '<label class="restore-dr-role-toggle"><input type="checkbox" class="node-role node-role-osd" value="osd"><span><b>●</b> OSD</span></label>' +
      '</div>' +
      '<label class="restore-dr-disk-field" hidden><span>OSD Disks <button type="button" class="field-info" title="Nhập một hoặc nhiều đĩa, phân tách bằng dấu phẩy">ℹ️</button></span><input type="text" class="node-osd-disk" placeholder="/dev/vdc, /dev/vdd" disabled></label>';
    if (ip) card.querySelector(".node-ip").value = ip;
    card.querySelector(".restore-dr-remove-node").addEventListener("click", function () { card.remove(); updateNodeNumbers(); });
    var osdCheckbox = card.querySelector(".node-role-osd");
    var diskField = card.querySelector(".restore-dr-disk-field");
    var diskInput = card.querySelector(".node-osd-disk");
    osdCheckbox.addEventListener("change", function () {
      diskField.hidden = !osdCheckbox.checked;
      diskInput.disabled = !osdCheckbox.checked;
      if (!osdCheckbox.checked) diskInput.value = "";
    });
    nodeRowsEl.appendChild(card);
    updateNodeNumbers();
  }

  function updateNodeNumbers() {
    if (!nodeRowsEl) return;
    Array.prototype.forEach.call(nodeRowsEl.querySelectorAll(".restore-dr-node-card"), function (card, index) {
      card.querySelector(".restore-dr-node-number").textContent = "NODE " + String(index + 1).padStart(2, "0");
    });
    updateNodeCount();
  }
  if (addNodeBtn) addNodeBtn.addEventListener("click", function () { addNodeCard(); });
  if (nodeRowsEl && nodeRowsEl.children.length === 0) { addNodeCard(); addNodeCard(); addNodeCard(); }

  function collectNodes() {
    if (!nodeRowsEl) return [];
    var nodes = [];
    Array.prototype.forEach.call(nodeRowsEl.querySelectorAll(".restore-dr-node-card"), function (card) {
      var ipInput = card.querySelector(".node-ip");
      var ip = ipInput ? ipInput.value.trim() : "";
      if (!ip) return;
      var roles = [];
      Array.prototype.forEach.call(card.querySelectorAll(".node-role:checked"), function (checkbox) { roles.push(checkbox.value); });
      var node = { ip: ip, roles: roles };
      if (roles.indexOf("osd") !== -1) {
        var raw = card.querySelector(".node-osd-disk").value.trim();
        node.osd_disks = raw ? raw.split(",").map(function (disk) { return disk.trim(); }).filter(Boolean) : [];
      }
      nodes.push(node);
    });
    return nodes;
  }

  // --- Version picker --------------------------------------------------
  var versionInput = document.getElementById("rf-version");
  var codenameSelect = document.getElementById("rf-codename");
  var versionSelect = document.getElementById("rf-version-select");
  var versionsByCodenameEl = document.getElementById("versions-by-codename-data");
  var errorEl = document.getElementById("rf-error");
  if (codenameSelect && versionSelect && versionsByCodenameEl) {
    var versionsByCodename = JSON.parse(versionsByCodenameEl.textContent || "{}");
    codenameSelect.addEventListener("change", function () {
      var versions = versionsByCodename[codenameSelect.value] || [];
      versionSelect.innerHTML = "";
      if (!codenameSelect.value || versions.length === 0) {
        versionSelect.disabled = true;
        var placeholder = document.createElement("option"); placeholder.value = ""; placeholder.textContent = "— Chọn dòng release trước —"; versionSelect.appendChild(placeholder); return;
      }
      versionSelect.disabled = false;
      for (var i = versions.length - 1; i >= 0; i -= 1) { var option = document.createElement("option"); option.value = versions[i]; option.textContent = versions[i]; versionSelect.appendChild(option); }
      if (versionInput) versionInput.value = versions[versions.length - 1];
    });
    versionSelect.addEventListener("change", function () { if (versionInput && versionSelect.value) versionInput.value = versionSelect.value; });
  }

  // --- Propose submit --------------------------------------------------
  if (form) form.addEventListener("submit", function (event) {
    event.preventDefault();
    if (errorEl) { errorEl.hidden = true; errorEl.textContent = ""; }
    if (versionInput && !versionInput.reportValidity()) { showStep(1); return; }
    if (!window.confirm("Xác nhận đề xuất KHÔI PHỤC CỤM SAU THẢM HỌA? Thao tác này sẽ dựng cụm mới và ghi đè dữ liệu RBD trên các node vừa điền bằng dữ liệu từ backup.")) return;

    var payload = { version: versionInput ? versionInput.value.trim() : "", nodes: collectNodes(), public_network: document.getElementById("rf-public-network").value.trim(), cluster_network: document.getElementById("rf-cluster-network").value.trim(), osd_pool_default_size: parseInt(document.getElementById("rf-pool-size").value, 10) || 3, osd_pool_default_min_size: parseInt(document.getElementById("rf-pool-min-size").value, 10) || 2 };
    if (submitBtn) { submitBtn.disabled = true; submitBtn.innerHTML = '<span class="restore-dr-spinner" aria-hidden="true"></span> Đang phân tích...'; }
    fetch("/restore-cluster/propose", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) })
      .then(function (response) { if (!response.ok) return response.json().then(function (data) { throw new Error(data.detail || "HTTP " + response.status); }); return response.json(); })
      .then(function () { window.location.reload(); })
      .catch(function (err) { if (submitBtn) { submitBtn.disabled = false; submitBtn.innerHTML = "<span>🔄</span> Đề xuất khôi phục"; } if (errorEl) { errorEl.textContent = err.message || "Không tạo được đề xuất khôi phục"; errorEl.hidden = false; } });
  });

  // --- Progress and responsive log drawer -----------------------------
  var logBox = document.getElementById("rf-log-box");
  var progressBarFill = document.getElementById("rf-progress-bar-fill");
  var progressBar = document.getElementById("rf-progress-bar");
  var progressLabel = document.getElementById("rf-progress-label");
  var logTitle = document.getElementById("rf-log-title");
  var clearBtn = document.getElementById("rf-log-clear");
  var copyBtn = document.getElementById("rf-log-copy");
  var logPanel = document.getElementById("restore-log-panel");

  function renderEmptyLog() {
    if (!logBox) return;
    logBox.innerHTML = '<div class="restore-dr-empty-log-icon" aria-hidden="true">📋</div><strong>Chưa có tiến trình</strong><span>Điền form và bấm “Đề xuất khôi phục” để bắt đầu.</span>';
    logBox.className = "restore-dr-empty-log";
  }
  function renderProgress(status, progress) {
    if (!logBox) return;
    if (!progress || !progress.length) { renderEmptyLog(); return; }
    logBox.className = "deploy-log-box"; logBox.innerHTML = "";
    var runningStep = null;
    progress.forEach(function (step) {
      var line = document.createElement("p"); line.className = "deploy-log-line status-" + step.status;
      var clockText = step.status === "running" ? nowClock() : (step.status === "done" || step.status === "failed") ? step.finished_at_display : null;
      line.innerHTML = (clockText ? '<span class="deploy-log-time">[' + escapeHtml(clockText) + "]</span> " : "") + (STATUS_GLYPH[step.status] || "•") + " " + escapeHtml(step.label || step.step) + (step.message ? " — " + escapeHtml(step.message) : ""); logBox.appendChild(line);
      if (step.hosts && step.hosts.length) step.hosts.forEach(function (host) { var hostLine = document.createElement("p"); hostLine.className = "deploy-log-line status-" + host.status; hostLine.style.marginLeft = "1.5em"; hostLine.innerHTML = (STATUS_GLYPH[host.status] || "•") + " " + escapeHtml(host.host) + (host.message ? " — " + escapeHtml(host.message) : ""); logBox.appendChild(hostLine); });
      if (step.status === "running") runningStep = step;
    });
    logBox.scrollTop = logBox.scrollHeight;
    var lastDone = progress.filter(function (step) { return step.status === "done"; }).pop(); var pct = runningStep ? runningStep.pct : (lastDone ? lastDone.pct : 0); var failedStep = progress.filter(function (step) { return step.status === "failed"; })[0]; if (failedStep) pct = failedStep.pct;
    if (progressBarFill) { progressBarFill.style.width = pct + "%"; progressBarFill.classList.toggle("is-active", !!runningStep); } if (progressBar) progressBar.setAttribute("aria-valuenow", String(pct));
    if (progressLabel) progressLabel.textContent = failedStep ? pct + "% — Lỗi ở bước: " + (failedStep.label || failedStep.step) : runningStep ? pct + "% — " + (runningStep.label || runningStep.step) : pct + "%";
    if (logTitle) { if (status === "EXECUTED") logTitle.textContent = "✅ Hoàn tất"; else if (status === "FAILED") logTitle.textContent = "❌ Thất bại"; else if (status === "APPROVED") logTitle.textContent = "Đang khôi phục"; }
  }
  var pollTimer = null;
  function pollOnce() { fetch("/restore-cluster/progress", { credentials: "same-origin" }).then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); }).then(function (data) { renderProgress(data.status, data.progress); if (data.status && TERMINAL_STATUSES.indexOf(data.status) !== -1) { if (pollTimer) clearInterval(pollTimer); window.location.reload(); } }).catch(function () {}); }
  if (logBox) { renderProgress(initialState.status, initialState.progress); if (initialState.status === "APPROVED") { pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS); pollOnce(); } }
  if (clearBtn && logBox) clearBtn.addEventListener("click", renderEmptyLog);
  if (copyBtn && logBox) copyBtn.addEventListener("click", function () { var text = logBox.innerText || logBox.textContent || ""; if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text); });
  Array.prototype.forEach.call(document.querySelectorAll("[data-open-restore-log]"), function (button) { button.addEventListener("click", function () { document.body.classList.add("restore-dr-log-drawer-open"); }); });
  Array.prototype.forEach.call(document.querySelectorAll("[data-close-restore-log]"), function (button) { button.addEventListener("click", function () { document.body.classList.remove("restore-dr-log-drawer-open"); }); });
})();
