(function () {
  var form = document.getElementById("deploy-form");
  var initialStateEl = document.getElementById("deploy-initial-state");
  if (!initialStateEl) {
    return; // not on /deploy-cluster
  }

  var initialState = JSON.parse(initialStateEl.textContent || "{}");
  var notYetSupported = initialState.not_yet_supported_methods || [];
  var deployPage = document.querySelector(".deploy-cluster-page");
  var clusterId = deployPage ? deployPage.dataset.clusterId : "";
  var realtimeAlert = document.getElementById("df-realtime-alert");

  var POLL_INTERVAL_MS = 2500;
  var TERMINAL_STATUSES = ["EXECUTED", "FAILED"];
  var FOLLOW_STATUSES = ["PENDING_APPROVAL", "APPROVED", "EXECUTING", "GRACE_PENDING", "INCONCLUSIVE"];
  // A terminal last_action is displayed as history only. It must not become
  // the active WebSocket/poll target: the next cluster-state event would
  // otherwise fetch the same terminal action and reload this page forever.
  var followAction = FOLLOW_STATUSES.indexOf(initialState.status) !== -1;
  var activeActionId = followAction && initialState.action_id
    ? String(initialState.action_id)
    : "";

  var copySshPublicKeyButton = document.getElementById("df-copy-ssh-public-key");
  if (copySshPublicKeyButton) copySshPublicKeyButton.addEventListener("click", function () {
    var publicKey = document.getElementById("df-ssh-public-key");
    var status = document.getElementById("df-ssh-key-copy-status");
    if (!publicKey) return;
    function copied() {
      var originalText = "📋 Copy public key";
      if (status) status.textContent = "Đã copy public key. Thêm key này vào authorized_keys của SSH User trên từng node.";
      copySshPublicKeyButton.textContent = "Đã copy ✓";
      window.setTimeout(function () {
        copySshPublicKeyButton.textContent = originalText;
        if (status) status.textContent = "Đây là public key của Worker. Private key không được mount vào Dashboard và không hiển thị trên trình duyệt.";
      }, 2000);
    }
    function selectForManualCopy() {
      publicKey.focus(); publicKey.select();
      if (status) status.textContent = "Clipboard không khả dụng; public key đã được chọn để copy thủ công.";
    }
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(publicKey.value).then(copied).catch(selectForManualCopy);
    } else {
      try { publicKey.focus(); publicKey.select(); if (document.execCommand("copy")) copied(); else selectForManualCopy(); }
      catch (_err) { selectForManualCopy(); }
    }
  });

  var STATUS_GLYPH = { pending: "⏳", running: "🔄", done: "✅", failed: "❌" };

  // Only show the Settings link for host-key failures. A plain refused or
  // timed-out connection needs a different fix, not a trust-store change.
  var HOST_KEY_PROVISION_RE = /Host key for server .* does not match|Server .* not found in known_hosts/;

  function pad2(n) { return String(n).padStart(2, "0"); }
  function nowClock() {
    var d = new Date();
    return pad2(d.getHours()) + ":" + pad2(d.getMinutes()) + ":" + pad2(d.getSeconds());
  }

  // --- Node table -----------------------------------------------------

  var nodeRowsEl = document.getElementById("node-rows");
  var addNodeBtn = document.getElementById("df-add-node");
  var nodeRowCount = 0;

  function addNodeRow(ip) {
    if (!nodeRowsEl) return;
    nodeRowCount += 1;
    var suggestedIp = "10.20.1." + (110 + nodeRowCount);
    var row = document.createElement("tr");
    row.innerHTML =
      '<td class="node-ip-cell"><input type="text" class="node-ip" placeholder="' + suggestedIp + '" value="' + escapeHtml(ip || "") + '" inputmode="decimal" autocomplete="off"><small class="node-field-error" hidden></small></td>' +
      '<td><label class="node-role-chip role-mon"><input type="checkbox" class="node-role" value="mon"><span>MON</span></label></td>' +
      '<td><label class="node-role-chip role-mgr"><input type="checkbox" class="node-role" value="mgr"><span>MGR</span></label></td>' +
      '<td><label class="node-role-chip role-osd"><input type="checkbox" class="node-role node-role-osd" value="osd"><span>OSD</span></label></td>' +
      '<td><label class="node-role-chip role-mds"><input type="checkbox" class="node-role" value="mds"><span>MDS</span></label></td>' +
      '<td><label class="node-role-chip role-rgw"><input type="checkbox" class="node-role" value="rgw"><span>RGW</span></label></td>' +
      '<td><input type="text" class="node-osd-disk" placeholder="/dev/vdc, /dev/vdd" disabled aria-label="Ổ đĩa OSD"></td>' +
      '<td><button type="button" class="btn btn-sm btn-ghost node-remove" aria-label="Xóa node" title="Xóa node">🗑</button></td>';
    row.querySelector(".node-remove").addEventListener("click", function () {
      row.remove();
      validateNodes(false);
      updateSubmitState();
    });
    var osdCheckbox = row.querySelector(".node-role-osd");
    var osdDiskInput = row.querySelector(".node-osd-disk");
    osdCheckbox.addEventListener("change", function () {
      osdDiskInput.disabled = !osdCheckbox.checked;
      if (!osdCheckbox.checked) osdDiskInput.value = "";
      updateSubmitState();
    });
    row.querySelector(".node-ip").addEventListener("input", function () {
      validateNodes(false);
      updateSubmitState();
    });
    Array.prototype.forEach.call(row.querySelectorAll(".node-role"), function (checkbox) {
      checkbox.addEventListener("change", function () {
        checkbox.closest(".node-role-chip").classList.toggle("is-selected", checkbox.checked);
        updateSubmitState();
      });
    });
    nodeRowsEl.appendChild(row);
  }

  if (addNodeBtn) {
    addNodeBtn.addEventListener("click", function () { addNodeRow(); });
  }
  if (nodeRowsEl && nodeRowsEl.children.length === 0) {
    addNodeRow();
    addNodeRow();
    addNodeRow();
  }

  var configToggle = document.getElementById("df-config-toggle");
  var configBody = document.getElementById("deploy-config-body");
  if (configToggle && configBody) {
    configToggle.addEventListener("click", function () {
      var expanded = configToggle.getAttribute("aria-expanded") === "true";
      configToggle.setAttribute("aria-expanded", expanded ? "false" : "true");
      configBody.hidden = expanded;
      var icon = configToggle.querySelector(".deploy-collapse-icon");
      if (icon) icon.textContent = expanded ? "▶" : "▼";
    });
  }

  function collectNodes() {
    if (!nodeRowsEl) return [];
    var nodes = [];
    Array.prototype.forEach.call(nodeRowsEl.querySelectorAll("tr"), function (row) {
      var ip = row.querySelector(".node-ip").value.trim();
      if (!ip) return;
      var roles = [];
      Array.prototype.forEach.call(row.querySelectorAll(".node-role:checked"), function (cb) {
        roles.push(cb.value);
      });
      var node = { ip: ip, roles: roles };
      if (roles.indexOf("osd") !== -1) {
        var diskInput = row.querySelector(".node-osd-disk");
        var rawDisks = diskInput ? diskInput.value.trim() : "";
        // Comma-separated so one node can carry multiple OSD disks (vd
        // "/dev/vdc, /dev/vdd") — split, trim, drop empties from stray
        // commas/whitespace.
        node.osd_disks = rawDisks
          ? rawDisks.split(",").map(function (d) { return d.trim(); }).filter(function (d) { return d.length > 0; })
          : [];
      }
      nodes.push(node);
    });
    return nodes;
  }

  function isValidIp(value) {
    if (!value) return false;
    if (value.indexOf(":") !== -1) {
      return /^[0-9a-f:]+$/i.test(value) && value.indexOf(":::") === -1;
    }
    var parts = value.split(".");
    return parts.length === 4 && parts.every(function (part) {
      return /^\d{1,3}$/.test(part) && Number(part) >= 0 && Number(part) <= 255;
    });
  }

  function setNodeError(row, message) {
    var input = row.querySelector(".node-ip");
    var error = row.querySelector(".node-field-error");
    if (input) input.classList.toggle("is-invalid", !!message);
    if (error) {
      error.textContent = message || "";
      error.hidden = !message;
    }
  }

  function validateNodes(showEmptyErrors) {
    if (!nodeRowsEl) return false;
    var rows = Array.prototype.slice.call(nodeRowsEl.querySelectorAll("tr"));
    var seen = {};
    var valid = rows.length > 0;
    var monCount = 0;
    rows.forEach(function (row) {
      var input = row.querySelector(".node-ip");
      var ip = input ? input.value.trim() : "";
      var message = "";
      if (!ip) {
        valid = false;
        if (showEmptyErrors) message = "Nhập IP của node";
      } else if (!isValidIp(ip)) {
        valid = false;
        message = "IP không đúng định dạng";
      } else if (seen[ip.toLowerCase()]) {
        valid = false;
        message = "IP bị trùng với node khác";
        setNodeError(seen[ip.toLowerCase()], "IP bị trùng với node khác");
      } else {
        seen[ip.toLowerCase()] = row;
      }
      setNodeError(row, message);
      if (row.querySelector('.node-role[value="mon"]:checked')) monCount += 1;
    });
    if (monCount === 0) valid = false;
    return valid;
  }

  function updateSubmitState() {
    var submitButton = document.getElementById("df-submit-btn");
    if (!submitButton) return;
    var hasVersion = !!(versionInput && versionInput.value.trim());
    var rows = nodeRowsEl ? nodeRowsEl.querySelectorAll("tr") : [];
    var hasNodeIp = Array.prototype.some.call(rows, function (row) {
      return row.querySelector(".node-ip") && row.querySelector(".node-ip").value.trim();
    });
    var hasMon = !!(nodeRowsEl && nodeRowsEl.querySelector('.node-role[value="mon"]:checked'));
    var poolSize = Number(document.getElementById("df-pool-size").value);
    var poolMinSize = Number(document.getElementById("df-pool-min-size").value);
    var poolValid = Number.isInteger(poolSize) && Number.isInteger(poolMinSize)
      && poolSize >= 1 && poolMinSize >= 1 && poolMinSize <= poolSize;
    var poolError = document.getElementById("df-pool-error");
    if (poolError) poolError.hidden = poolValid;
    document.getElementById("df-pool-min-size").classList.toggle("is-invalid", !poolValid);
    var valid = hasVersion && hasNodeIp && hasMon && validateNodes(false) && poolValid;
    submitButton.disabled = !valid;
    return valid;
  }

  // --- Method radio -> rpm-path field + not-yet-supported note --------

  var rpmPathLabel = document.getElementById("df-rpm-path-label");
  var errorEl = document.getElementById("df-error");

  function currentMethod() {
    var checked = document.querySelector('input[name="method"]:checked');
    return checked ? checked.value : "cephadm";
  }

  function onMethodChange() {
    var method = currentMethod();
    if (rpmPathLabel) rpmPathLabel.hidden = method !== "rpm-local";
    Array.prototype.forEach.call(document.querySelectorAll(".deploy-method-card"), function (card) {
      card.classList.toggle("is-selected", !!card.querySelector("input[type=radio]:checked"));
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll('input[name="method"]'), function (radio) {
    radio.addEventListener("change", onMethodChange);
  });
  onMethodChange();

  // --- Version picker: chọn dòng release rồi chọn phiên bản -------------
  // Two dependent <select>s that just FILL df-version (the real, still
  // directly-editable text input the form submits) — same "convenience
  // filler, never the only way in" role the old flat version-chip buttons
  // had, now organized by release line (nautilus/octopus/.../reef/...)
  // instead of one flat hardcoded list of 4.

  var versionInput = document.getElementById("df-version");
  var codenameSelect = document.getElementById("df-codename");
  var versionSelect = document.getElementById("df-version-select");
  var versionsByCodenameEl = document.getElementById("versions-by-codename-data");

  if (codenameSelect && versionSelect && versionsByCodenameEl) {
    var versionsByCodename = JSON.parse(versionsByCodenameEl.textContent || "{}");

    codenameSelect.addEventListener("change", function () {
      var versions = versionsByCodename[codenameSelect.value] || [];
      versionSelect.innerHTML = "";
      if (!codenameSelect.value || versions.length === 0) {
        versionSelect.disabled = true;
        var placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "— Chọn phiên bản —";
        versionSelect.appendChild(placeholder);
        if (versionInput) versionInput.value = "";
        updateSubmitState();
        return;
      }
      versionSelect.disabled = false;
      // Newest point release of the line first — that's almost always
      // what an operator deploying/upgrading actually wants.
      for (var i = versions.length - 1; i >= 0; i--) {
        var option = document.createElement("option");
        option.value = versions[i];
        option.textContent = versions[i];
        versionSelect.appendChild(option);
      }
      if (versionInput) versionInput.value = versions[versions.length - 1];
      updateSubmitState();
    });

    versionSelect.addEventListener("change", function () {
      if (versionInput && versionSelect.value) versionInput.value = versionSelect.value;
      updateSubmitState();
    });
  }

  if (versionInput) versionInput.addEventListener("input", updateSubmitState);
  ["df-pool-size", "df-pool-min-size"].forEach(function (id) {
    var input = document.getElementById(id);
    if (input) input.addEventListener("input", updateSubmitState);
  });
  var luksToggle = document.getElementById("df-osd-encryption");
  if (luksToggle) luksToggle.addEventListener("change", function () {
    document.getElementById("df-luks-warning").hidden = !luksToggle.checked;
  });
  var monitoringToggle = document.getElementById("df-register-monitoring");
  if (monitoringToggle) monitoringToggle.addEventListener("change", function () {
    var nameField = document.getElementById("df-monitor-name-field");
    var nameInput = document.getElementById("df-monitor-name");
    nameField.hidden = !monitoringToggle.checked;
    nameInput.required = monitoringToggle.checked;
  });
  Array.prototype.forEach.call(document.querySelectorAll("[data-dialog-close]"), function (button) {
    button.addEventListener("click", function () {
      var dialog = button.closest("dialog");
      if (dialog) dialog.close();
    });
  });
  var restartDialog = document.getElementById("deploy-restart-dialog");
  var restartForm = document.getElementById("df-restart-form");
  var restartMessage = document.getElementById("df-restart-message");
  document.querySelectorAll("[data-restart-service]").forEach(function (button) {
    button.addEventListener("click", function () {
      var service = button.dataset.restartService;
      if (["worker", "watcher"].indexOf(service) === -1 || !restartDialog || !restartForm) return;
      restartForm.action = "/settings/restart-" + service;
      restartMessage.textContent = "Bạn có chắc muốn khởi động lại " + service.toUpperCase() + "? Dịch vụ có thể tạm ngừng xử lý trong lúc khởi động.";
      var menu = button.closest("details");
      if (menu) menu.open = false;
      restartDialog.showModal();
    });
  });
  updateSubmitState();

  var deployDialog = document.getElementById("deploy-confirm-dialog");
  var confirmWord = document.getElementById("df-confirm-word");
  var confirmSubmit = document.getElementById("df-confirm-submit");

  function collectPayload() {
    var payload = {
        version: versionInput ? versionInput.value.trim() : "",
        method: currentMethod(),
        rpm_path: document.getElementById("df-rpm-path") ? document.getElementById("df-rpm-path").value.trim() : "",
        nodes: collectNodes(),
        public_network: document.getElementById("df-public-network").value.trim(),
        cluster_network: document.getElementById("df-cluster-network").value.trim(),
        osd_pool_default_size: parseInt(document.getElementById("df-pool-size").value, 10) || 3,
        osd_pool_default_min_size: parseInt(document.getElementById("df-pool-min-size").value, 10) || 2,
        osd_encryption: Boolean(document.getElementById("df-osd-encryption") && document.getElementById("df-osd-encryption").checked)
      };
      var registerBox = document.getElementById("df-register-monitoring");
      if (registerBox && registerBox.checked) {
        payload.register_monitoring = true;
        payload.monitor_cluster_name = document.getElementById("df-monitor-name").value.trim();
        var stagingBox = document.getElementById("df-failure-lab-staging");
        if (stagingBox && stagingBox.checked && !stagingBox.disabled) payload.failure_lab_staging = true;
      }
    return payload;
  }

  function sendProposal(payload) {
      fetch("/deploy-cluster/propose", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      })
        .then(function (response) {
          if (!response.ok) {
            return response.json().then(function (data) {
              throw new Error(data.detail || "HTTP " + response.status);
            });
          }
          return response.json();
        })
        .then(function () {
          window.location.reload();
        })
        .catch(function (err) {
          if (errorEl) {
            errorEl.textContent = err.message || "Không tạo được đề xuất dựng cụm";
            errorEl.hidden = false;
          }
        });
  }

  function showDeploySummary(payload) {
    var summary = document.getElementById("df-confirm-summary");
    if (!summary) return;
    summary.replaceChildren();
    var nodeText = payload.nodes.map(function (node) {
      return node.ip + " (" + node.roles.map(function (role) { return role.toUpperCase(); }).join(", ") + ")";
    }).join("; ");
    var items = [
      ["Ceph", payload.version],
      ["Phương thức", payload.method],
      ["Nodes / roles", nodeText],
      ["Public / Cluster network", payload.public_network + " / " + (payload.cluster_network || payload.public_network)],
      ["dm-crypt (LUKS)", payload.osd_encryption ? "Bật" : "Tắt"]
    ];
    if (payload.register_monitoring) items.push(["Giám sát", payload.monitor_cluster_name || "Cụm thứ hai"]);
    if (payload.failure_lab_staging) items.push(["Failure Lab", "Cụm Staging (gây lỗi vẫn tắt)"]);
    items.forEach(function (item) {
      var term = document.createElement("dt");
      var value = document.createElement("dd");
      term.textContent = item[0];
      value.textContent = item[1] || "—";
      summary.append(term, value);
    });
  }

  if (confirmWord && confirmSubmit) {
    confirmWord.addEventListener("input", function () {
      confirmSubmit.disabled = confirmWord.value.trim() !== "DEPLOY";
    });
    confirmSubmit.addEventListener("click", function () {
      if (confirmWord.value.trim() !== "DEPLOY") return;
      deployDialog.close();
      sendProposal(collectPayload());
    });
  }

  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      if (errorEl) { errorEl.hidden = true; errorEl.textContent = ""; }
      if (!versionInput || !versionInput.value.trim()) {
        if (errorEl) { errorEl.textContent = "Chọn hoặc nhập phiên bản Ceph trước khi tiếp tục."; errorEl.hidden = false; }
        updateSubmitState();
        return;
      }
      if (!validateNodes(true)) {
        if (errorEl) { errorEl.textContent = "Kiểm tra lại IP node và chọn ít nhất một node MON."; errorEl.hidden = false; }
        updateSubmitState();
        return;
      }
      if (!updateSubmitState()) return;
      var method = currentMethod();
      if (notYetSupported.indexOf(method) !== -1) {
        if (errorEl) { errorEl.textContent = "Phương thức này chưa được hỗ trợ tự động — chọn cephadm."; errorEl.hidden = false; }
        return;
      }
      var payload = collectPayload();
      showDeploySummary(payload);
      if (confirmWord) confirmWord.value = "";
      if (confirmSubmit) confirmSubmit.disabled = true;
      if (deployDialog && deployDialog.showModal) deployDialog.showModal();
      else if (errorEl) { errorEl.textContent = "Trình duyệt không hỗ trợ hộp thoại xác nhận an toàn."; errorEl.hidden = false; }
      if (deployDialog && deployDialog.open && confirmWord) window.setTimeout(function () { confirmWord.focus(); }, 0);
    });
  }

  var retryButton = document.getElementById("df-retry-last");
  var lastActionParamsEl = document.getElementById("deploy-last-action-params-data");
  if (retryButton && lastActionParamsEl && form) {
    retryButton.addEventListener("click", function () {
      var params = JSON.parse(lastActionParamsEl.textContent || "{}");
      if (versionInput) versionInput.value = params.version || "";
      Array.prototype.forEach.call(document.querySelectorAll('input[name="method"]'), function (radio) {
        radio.checked = radio.value === (params.method || "cephadm");
      });
      if (document.getElementById("df-rpm-path")) document.getElementById("df-rpm-path").value = params.rpm_path || "";
      if (document.getElementById("df-public-network")) document.getElementById("df-public-network").value = params.public_network || "";
      if (document.getElementById("df-register-monitoring")) {
        document.getElementById("df-register-monitoring").checked = !!params.register_monitoring;
        document.getElementById("df-monitor-name").value = params.monitor_cluster_name || "";
        document.getElementById("df-monitor-name-field").hidden = !params.register_monitoring;
        if (document.getElementById("df-staging-field")) document.getElementById("df-staging-field").hidden = !params.register_monitoring;
        if (document.getElementById("df-failure-lab-staging")) document.getElementById("df-failure-lab-staging").checked = !!params.failure_lab_staging;
      }
      if (document.getElementById("df-cluster-network")) document.getElementById("df-cluster-network").value = params.cluster_network || "";
      onMethodChange();
      if (nodeRowsEl) {
        nodeRowsEl.innerHTML = "";
        nodeRowCount = 0;
      (params.nodes || []).forEach(function (node) {
          addNodeRow(node.ip || "");
          var row = nodeRowsEl.lastElementChild;
          (node.roles || []).forEach(function (role) {
            var checkbox = row.querySelector('.node-role[value="' + role + '"]');
            if (checkbox) checkbox.checked = true;
            if (checkbox) checkbox.closest(".node-role-chip").classList.add("is-selected");
          });
          var disks = (node.osd_disks || []).join(", ");
          var diskInput = row.querySelector(".node-osd-disk");
          if (diskInput) { diskInput.value = disks; diskInput.disabled = !(node.roles || []).includes("osd"); }
        });
        if (!nodeRowsEl.children.length) { addNodeRow(); addNodeRow(); addNodeRow(); }
      }
      var poolSizeInput = document.getElementById("df-pool-size");
      var poolMinInput = document.getElementById("df-pool-min-size");
      if (poolSizeInput) poolSizeInput.value = params.osd_pool_default_size || 3;
      if (poolMinInput) poolMinInput.value = params.osd_pool_default_min_size || 2;
      if (luksToggle) {
        luksToggle.checked = !!params.osd_encryption;
        document.getElementById("df-luks-warning").hidden = !luksToggle.checked;
      }
      var configCard = document.getElementById("deploy-config-card");
      var configBody = document.getElementById("deploy-config-body");
      var configToggle = document.getElementById("df-config-toggle");
      if (configBody) configBody.hidden = false;
      if (configToggle) { configToggle.setAttribute("aria-expanded", "true"); configToggle.querySelector(".deploy-collapse-icon").textContent = "▼"; }
      updateSubmitState();
      if (configCard) configCard.scrollIntoView({ behavior: "smooth", block: "start" });
    });
  }

  // --- Progress polling + terminal log rendering -------------------------

  var logBox = document.getElementById("df-log-box");
  var progressBarFill = document.getElementById("df-progress-bar-fill");
  var progressBar = document.getElementById("df-progress-bar");
  var progressLabel = document.getElementById("df-progress-label");
  var logTitle = document.getElementById("df-log-title");
  var logCard = document.getElementById("deploy-log-card");
  var clearBtn = document.getElementById("df-log-clear");
  var copyBtn = document.getElementById("df-log-copy");
  var downloadBtn = document.getElementById("df-log-download");
  var logAutoFollow = true;
  var logSearch = document.getElementById("df-log-search");
  var activeLogFilter = "all";

  if (logBox) logBox.addEventListener("scroll", function () {
    logAutoFollow = logBox.scrollHeight - logBox.scrollTop - logBox.clientHeight < 48;
  });

  function applyLogFilters() {
    if (!logBox) return;
    var query = (logSearch ? logSearch.value : "").trim().toLowerCase();
    Array.prototype.forEach.call(logBox.querySelectorAll(".deploy-log-line"), function (line) {
      line.querySelectorAll("mark.deploy-log-match").forEach(function (mark) {
        mark.replaceWith(document.createTextNode(mark.textContent));
      });
      var text = line.textContent || "";
      var matchesLevel = activeLogFilter === "all"
        || (activeLogFilter === "error" && line.classList.contains("status-failed"))
        || (activeLogFilter === "warning" && /warn|warning|⚠/i.test(text));
      var matchesQuery = !query || text.toLowerCase().indexOf(query) !== -1;
      line.hidden = !(matchesLevel && matchesQuery);
      if (!line.hidden && query) {
        var walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
        var textNodes = [];
        while (walker.nextNode()) textNodes.push(walker.currentNode);
        textNodes.forEach(function (node) {
          var lower = node.nodeValue.toLowerCase();
          var index = lower.indexOf(query);
          if (index < 0) return;
          var fragment = document.createDocumentFragment();
          var cursor = 0;
          while (index >= 0) {
            if (index > cursor) fragment.appendChild(document.createTextNode(node.nodeValue.slice(cursor, index)));
            var mark = document.createElement("mark");
            mark.className = "deploy-log-match";
            mark.textContent = node.nodeValue.slice(index, index + query.length);
            fragment.appendChild(mark);
            cursor = index + query.length;
            index = lower.indexOf(query, cursor);
          }
          if (cursor < node.nodeValue.length) fragment.appendChild(document.createTextNode(node.nodeValue.slice(cursor)));
          node.replaceWith(fragment);
        });
      }
    });
  }

  if (logSearch) logSearch.addEventListener("input", applyLogFilters);
  document.querySelectorAll("[data-log-filter]").forEach(function (button) {
    button.addEventListener("click", function () {
      activeLogFilter = button.dataset.logFilter;
      document.querySelectorAll("[data-log-filter]").forEach(function (item) {
        var selected = item === button;
        item.classList.toggle("is-active", selected);
        item.setAttribute("aria-pressed", selected ? "true" : "false");
      });
      applyLogFilters();
    });
  });

  function renderProgress(status, progress) {
    if (logCard && status) logCard.dataset.status = status;
    if (!logBox) return; // PENDING_APPROVAL view has no log box (shows the plan instead)

    if (!progress || !progress.length) {
      if (status === "FAILED") {
        logBox.innerHTML = "";
        var fallbackLine = document.createElement("p");
        fallbackLine.className = "deploy-log-line status-failed";
        fallbackLine.textContent = "❌ Deployment thất bại nhưng không có log chi tiết. Hãy kiểm tra Worker log hoặc liên hệ admin.";
        logBox.appendChild(fallbackLine);
        if (progressLabel) progressLabel.textContent = "0% — Deployment thất bại; thiếu log chi tiết";
        if (logTitle) logTitle.textContent = "❌ Thất bại";
      } else if (status === "EXECUTED") {
        if (progressBarFill) { progressBarFill.style.width = "100%"; progressBarFill.classList.add("is-complete"); }
        if (progressBar) progressBar.setAttribute("aria-valuenow", "100");
        if (progressLabel) progressLabel.textContent = "100% — Hoàn tất";
        if (logTitle) logTitle.textContent = "✅ Hoàn tất";
      } else if (["APPROVED", "EXECUTING", "GRACE_PENDING", "INCONCLUSIVE"].indexOf(status) !== -1) {
        if (logTitle) { logTitle.textContent = "Đang chạy…"; logTitle.classList.add("is-running"); }
      }
      return;
    }

    var shouldFollowLog = logAutoFollow;
    logBox.innerHTML = "";
    var runningStep = null;
    progress.forEach(function (step) {
      var glyph = STATUS_GLYPH[step.status] || "•";
      var line = document.createElement("p");
      line.className = "deploy-log-line status-" + step.status;
      // 2026-07-28 fix: this used to be nowClock() unconditionally, which
      // rewrote EVERY line's timestamp to the browser's current clock on
      // every single poll tick (renderProgress rebuilds the whole log box
      // from scratch each time) — a step that had already finished kept
      // showing a drifting "now" instead of freezing at when it actually
      // finished. finished_at_display/started_at_display are computed
      // server-side (dashboard/routes/deploy_cluster.py) from the step's
      // own real, frozen timestamps — only the step CURRENTLY running still
      // ticks live (there is no "finished" moment for it yet to freeze at).
      var clockText = step.status === "running"
        ? nowClock()
        : (step.status === "done" || step.status === "failed") ? step.finished_at_display : null;
      var timeSpan = clockText ? "<span class=\"deploy-log-time\">[" + clockText + "]</span> " : "";
      line.innerHTML = timeSpan + glyph + " " + escapeHtml(step.label || step.step);
      var duplicateHostMessage = (step.hosts || []).some(function (h) {
        return h.message && h.message === step.message;
      });
      if (step.message && !duplicateHostMessage) {
        line.innerHTML += " — " + escapeHtml(step.message);
      }
      logBox.appendChild(line);
      if (step.hosts && step.hosts.length) {
        step.hosts.forEach(function (h) {
          var hostGlyph = STATUS_GLYPH[h.status] || "•";
          var hostLine = document.createElement("p");
          hostLine.className = "deploy-log-line status-" + h.status;
          hostLine.style.marginLeft = "1.5em";
          var hostClock = h.finished_at_display || h.started_at_display || "";
          hostLine.innerHTML = (hostClock ? "<span class=\"deploy-log-time\">[" + escapeHtml(hostClock) + "]</span> " : "") + hostGlyph + " " + escapeHtml(h.host) + (h.message ? " — " + escapeHtml(h.message) : "");
          logBox.appendChild(hostLine);
        });
      }
      if (step.status === "failed" && step.message && HOST_KEY_PROVISION_RE.test(step.message)) {
        var failedHost = (step.hosts || []).filter(function (h) { return h.status === "failed"; })[0];
        if (failedHost) renderProvisionHostKeyControl(
          failedHost.host,
          /does not match/.test(step.message)
        );
      }
      if (step.status === "running") runningStep = step;
    });
    var lastDone = progress.filter(function (s) { return s.status === "done"; }).pop();
    var pct = runningStep ? runningStep.pct : (lastDone ? lastDone.pct : 0);
    var failedStep = progress.filter(function (s) { return s.status === "failed"; })[0];
    if (failedStep) pct = failedStep.pct;
    if (status === "EXECUTED") pct = 100;

    if (progressBarFill) {
      progressBarFill.style.width = pct + "%";
      progressBarFill.classList.toggle("is-active", !!runningStep);
      progressBarFill.classList.toggle("is-complete", status === "EXECUTED");
    }
    if (progressBar) progressBar.setAttribute("aria-valuenow", String(pct));
    if (progressLabel) {
      if (failedStep) {
        progressLabel.textContent = pct + "% — Lỗi ở bước: " + (failedStep.label || failedStep.step);
      } else if (runningStep) {
        progressLabel.textContent = pct + "% — " + (runningStep.label || runningStep.step);
      } else {
        progressLabel.textContent = pct + "%";
      }
    }
    if (logTitle) {
      logTitle.classList.toggle("is-running", !!runningStep || ["APPROVED", "EXECUTING", "GRACE_PENDING", "INCONCLUSIVE"].indexOf(status) !== -1);
      if (status === "EXECUTED") logTitle.textContent = "✅ Hoàn tất";
      else if (status === "FAILED") logTitle.textContent = "❌ Thất bại";
      else if (["APPROVED", "EXECUTING", "GRACE_PENDING", "INCONCLUSIVE"].indexOf(status) !== -1) logTitle.textContent = "Đang chạy…";
    }
    applyLogFilters();
    if (shouldFollowLog) logBox.scrollTop = logBox.scrollHeight;
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = String(text == null ? "" : text);
    return div.innerHTML;
  }

  function renderProvisionHostKeyControl(host, changed) {
    var box = document.createElement("p");
    box.className = "deploy-log-line status-failed";
    box.style.marginLeft = "1.5em";
    box.appendChild(document.createTextNode(
      (changed
        ? "⚠ Node " + host + " đang có host key cũ không khớp với key hiện tại. Hãy xác minh fingerprint rồi cập nhật key trong Settings. "
        : "⚠ Node " + host + " chưa có host key tin cậy. Hãy xác minh fingerprint rồi nhập key trong Settings. ")
    ));

    if (!initialState.is_admin) {
      box.appendChild(document.createTextNode(
        "Cần tài khoản admin để quản lý host key."
      ));
      logBox.appendChild(box);
      return;
    }
    var link = document.createElement("a");
    link.href = "/settings?section=ceph-host-keys&host=" + encodeURIComponent(host);
    link.textContent = changed
      ? "Mở Settings để cập nhật host key cho " + host
      : "Mở Settings để nhập host key cho " + host;
    box.appendChild(link);
    logBox.appendChild(box);
  }

  var pollTimer = null;
  var pollInFlight = false;

  function pollOnce() {
    if (pollInFlight) return;
    pollInFlight = true;
    fetch("/deploy-cluster/progress", { credentials: "same-origin" })
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        if (data.action_id) activeActionId = String(data.action_id);
        renderProgress(data.status, data.progress);
        if (followAction && data.status && TERMINAL_STATUSES.indexOf(data.status) !== -1) {
          if (pollTimer) clearInterval(pollTimer);
          window.location.reload();
        }
      })
      .catch(function () {
        // Transient network hiccup — next tick retries; no need to surface
        // this as a hard error the way a propose validation failure is.
      })
      .finally(function () { pollInFlight = false; });
  }

  if (logBox) {
    renderProgress(initialState.status, initialState.progress);
    // Poll an action that is still in-flight. A resolved last_action is only
    // historical output and must never trigger the terminal reload path.
    if (["APPROVED", "EXECUTING", "GRACE_PENDING", "INCONCLUSIVE"].indexOf(initialState.status) !== -1) {
      pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
      pollOnce();
    }
  }

  if (clearBtn && logBox) {
    clearBtn.addEventListener("click", function () { logBox.innerHTML = ""; });
  }
  if (copyBtn && logBox) {
    copyBtn.addEventListener("click", function () {
      var text = logBox.innerText || logBox.textContent || "";
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text);
      }
    });
  }
  if (downloadBtn && logBox) {
    downloadBtn.addEventListener("click", function () {
      var content = Array.prototype.map.call(logBox.querySelectorAll(".deploy-log-line"), function (line) {
        return line.textContent.trim();
      }).join("\n");
      var blobUrl = URL.createObjectURL(new Blob([content + "\n"], { type: "text/plain;charset=utf-8" }));
      var link = document.createElement("a");
      link.href = blobUrl;
      link.download = "ceph-deploy-" + new Date().toISOString().replace(/[:.]/g, "-") + ".log";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(function () { URL.revokeObjectURL(blobUrl); }, 1000);
    });
  }

  if (clusterId && window.CephClusterState) {
    var unsubscribe = window.CephClusterState.subscribe(clusterId, function (event) {
      if (activeActionId && event.action_id && String(event.action_id) !== activeActionId) return;
      if (!activeActionId && event.action_id) return;
      if (event.event === "snapshot_refresh_failed") {
        if (realtimeAlert) {
          realtimeAlert.textContent = "⚠ Post-check deploy chưa xác nhận được thay đổi; log hiện tại vẫn được giữ nguyên.";
          realtimeAlert.hidden = false;
        }
        pollOnce();
      } else if (event.event === "snapshot_changed") {
        if (realtimeAlert) { realtimeAlert.hidden = true; realtimeAlert.textContent = ""; }
        pollOnce();
      } else if (event.event === "action_state_changed") {
        pollOnce();
      }
    });
    window.addEventListener("pagehide", function () { if (unsubscribe) unsubscribe(); });
  }
})();

// Deploy Cluster: the monitored-cluster name is only asked when the operator
// chooses to register the new cluster as an additional monitored cluster.
(function () {
  var box = document.getElementById("df-register-monitoring");
  var field = document.getElementById("df-monitor-name-field");
  if (!box || !field) return;
  var staging = document.getElementById("df-staging-field");
  box.addEventListener("change", function () {
    field.hidden = !box.checked;
    if (staging) staging.hidden = !box.checked;
  });
})();
