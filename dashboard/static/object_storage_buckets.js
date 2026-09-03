(function () {
  var tabs = Array.prototype.slice.call(document.querySelectorAll("[data-bucket-tab]"));
  if (!tabs.length) return;
  var panels = Array.prototype.slice.call(document.querySelectorAll(".bucket-feature-panel"));

  function activate(tab) {
    tabs.forEach(function (item) {
      var active = item === tab;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-selected", String(active));
      item.tabIndex = active ? 0 : -1;
    });
    panels.forEach(function (panel) { panel.hidden = panel.id !== tab.dataset.bucketTab; });
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
  var button = document.getElementById("bucket-delete-all");
  if (!button) return;
  var status = document.getElementById("bucket-delete-all-status");
  button.addEventListener("click", function () {
    button.disabled = true;
    status.textContent = "Đang purge object và xóa tất cả bucket...";
    fetch("/api/object-storage/buckets/delete-all?cluster=" + encodeURIComponent(button.dataset.cluster), {method: "POST"})
      .then(function (response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Thao tác thất bại"); }); })
      .then(function (data) { status.textContent = "Đã xóa " + data.deleted_count + " bucket. Request ID: " + data.request_id; window.location.reload(); })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; button.disabled = false; });
  });
})();

(function () {
  var capabilityStatus = document.getElementById("object-storage-capability-status");
  if (!capabilityStatus) return;
  var source = document.getElementById("bucket-create-form");
  function disableForm(id, reason) {
    var form = document.getElementById(id);
    if (!form) return;
    form.querySelectorAll("button, input, select, textarea").forEach(function (control) { control.disabled = true; control.title = reason; });
  }
  fetch("/api/object-storage/capabilities?cluster=" + encodeURIComponent(source.dataset.cluster))
    .then(function (response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Không đọc được capability"); }); })
    .then(function (data) {
      capabilityStatus.textContent = "Ceph " + data.ceph_version + " (" + data.ceph_release + ") · Chỉ các tính năng tương thích bên dưới được phép thao tác.";
      var create = data.bucket_create;
      var governance = data.bucket_governance;
      var lifecycle = data.lifecycle;
      if (!create.placement_supported) {
        ["bucket-create-api-name", "bucket-create-placement"].forEach(function (id) { document.getElementById(id).disabled = true; });
        document.getElementById("bucket-create-placement-label").title = create.placement_unavailable_reason;
        document.getElementById("bucket-create-api-name-label").title = create.placement_unavailable_reason;
      }
      if (!governance.object_lock_at_create) {
        document.getElementById("bucket-create-object-lock").disabled = true;
        document.getElementById("bucket-create-object-lock-label").title = governance.object_lock_unavailable_reason;
      }
      document.querySelectorAll("#bucket-governance-action option[data-capability='versioning']").forEach(function (option) { option.disabled = !governance.versioning; option.title = governance.versioning_unavailable_reason || ""; });
      document.querySelectorAll("#bucket-governance-action option[data-capability='object-lock']").forEach(function (option) { option.disabled = !governance.default_retention; option.title = governance.object_lock_unavailable_reason || ""; });
      if (!lifecycle.supported) {
        disableForm("bucket-lifecycle-form", lifecycle.unavailable_reason);
        document.getElementById("bucket-lifecycle-status").textContent = lifecycle.unavailable_reason;
      } else if (!lifecycle.transition_supported) {
        document.querySelectorAll("[data-capability='lifecycle-transition']").forEach(function (option) { option.disabled = true; option.title = lifecycle.transition_unavailable_reason; });
        document.getElementById("bucket-lifecycle-status").textContent = lifecycle.transition_unavailable_reason + " Các rule expiration vẫn được phép.";
      }
      if (!data.bucket_policy_acl.supported) {
        disableForm("bucket-policy-form", data.bucket_policy_acl.unavailable_reason);
        document.getElementById("bucket-policy-status").textContent = data.bucket_policy_acl.unavailable_reason;
      }
    })
    .catch(function (error) {
      capabilityStatus.textContent = "Không xác định được phiên bản/capability Ceph: " + error.message + ". Các thao tác ghi đã bị khóa.";
      ["bucket-create-form", "bucket-governance-form", "bucket-lifecycle-form", "bucket-policy-form", "bucket-delete-form"].forEach(function (id) { disableForm(id, error.message); });
    });
})();

(function () {
  var form = document.getElementById("bucket-delete-form");
  if (!form) return;
  var preview = document.getElementById("bucket-delete-preview");
  var impact = document.getElementById("bucket-delete-impact");
  var warning = document.getElementById("bucket-delete-warning");
  var confirmation = document.getElementById("bucket-delete-confirmation");
  var execute = document.getElementById("bucket-delete-execute");
  var status = document.getElementById("bucket-delete-status");
  var approved = null;
  function payload() { return {action: document.getElementById("bucket-delete-action").value, bucket: document.getElementById("bucket-delete-name").value.trim(), owner: document.getElementById("bucket-delete-owner").value.trim(), endpoint: document.getElementById("bucket-delete-endpoint").value.trim()}; }
  function endpoint(kind) { return "/api/object-storage/buckets/delete/" + kind + "?cluster=" + encodeURIComponent(form.dataset.cluster); }
  function parse(response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Thao tác thất bại"); }); }
  form.addEventListener("submit", function (event) {
    event.preventDefault(); approved = null; preview.hidden = true; execute.disabled = true; status.textContent = "Đang kiểm tra object, version và delete marker...";
    var body = payload();
    fetch(endpoint("preview"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(parse).then(function (data) { impact.textContent = "Ceph " + data.ceph_version + " (" + data.ceph_release + ") · rủi ro " + data.risk + "\nObjects: " + data.impact.object_count + "\nDung lượng: " + (data.impact.size || data.impact.size_bytes + " bytes") + "\nSample versions: " + data.impact.sample_versions + "\nSample delete markers: " + data.impact.sample_delete_markers + (data.impact.sample_truncated ? "\nSample bị giới hạn ở 1.000 entries." : ""); warning.textContent = data.blocked_reason || data.retention_warning; preview.hidden = false; confirmation.value = ""; if (data.allowed) { approved = Object.assign({}, body, {expected_objects: data.expected_objects, expected: data.confirmation_required}); status.textContent = "Nhập chính xác " + data.confirmation_required + " để xác nhận."; } else { status.textContent = "Không thể thực thi: " + data.blocked_reason; } })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; });
  });
  confirmation.addEventListener("input", function () { execute.disabled = !approved || confirmation.value !== approved.expected; });
  execute.addEventListener("click", function () {
    if (!approved) return; execute.disabled = true; status.textContent = "Đang xóa vĩnh viễn...";
    var body = Object.assign({}, approved, {confirmation: confirmation.value}); delete body.expected;
    fetch(endpoint("execute"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(parse).then(function (data) { status.textContent = "Đã xóa bucket. Request ID: " + data.request_id; approved = null; preview.hidden = true; window.location.reload(); })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; execute.disabled = false; });
  });
})();

(function () {
  var form = document.getElementById("bucket-policy-form");
  if (!form) return;
  var action = document.getElementById("bucket-policy-action");
  var policyLabel = document.getElementById("bucket-policy-json-label");
  var aclLabel = document.getElementById("bucket-policy-acl-label");
  var bucket = document.getElementById("bucket-policy-name");
  var owner = document.getElementById("bucket-policy-owner");
  var gateway = document.getElementById("bucket-policy-endpoint");
  var submit = document.getElementById("bucket-policy-submit");
  var generated = document.getElementById("bucket-policy-generated-json");
  var preview = document.getElementById("bucket-policy-preview");
  var diff = document.getElementById("bucket-policy-diff");
  var warning = document.getElementById("bucket-policy-warning");
  var confirmation = document.getElementById("bucket-policy-confirmation");
  var execute = document.getElementById("bucket-policy-execute");
  var status = document.getElementById("bucket-policy-status");
  var approved = null;
  function ready() { return Boolean(action.value && bucket.value.trim() && owner.value.trim() && gateway.value.trim() && gateway.checkValidity()); }
  function buildPolicy() {
    var bucketArn = "arn:aws:s3:::" + bucket.value.trim();
    var resourceChoice = document.getElementById("bucket-policy-resource").value;
    var resources = resourceChoice === "both" ? [bucketArn, bucketArn + "/*"] : [resourceChoice === "bucket" ? bucketArn : bucketArn + "/*"];
    var principal = document.getElementById("bucket-policy-principal").value === "public" ? "*" : {AWS: "arn:aws:iam:::user/" + owner.value.trim()};
    return {Version: "2012-10-17", Statement: [{Sid: "DashboardManagedRule", Effect: document.getElementById("bucket-policy-effect").value, Principal: principal, Action: [document.getElementById("bucket-policy-s3-action").value], Resource: resources}]};
  }
  function refresh() {
    var baseReady = ready();
    policyLabel.hidden = !baseReady || action.value !== "policy_put";
    aclLabel.hidden = !baseReady || action.value !== "acl_set";
    submit.hidden = !baseReady;
    if (baseReady && action.value === "policy_put") generated.textContent = JSON.stringify(buildPolicy(), null, 2);
    preview.hidden = true; approved = null;
  }
  function payload() {
    return {action: action.value, bucket: bucket.value.trim(), owner: owner.value.trim(), endpoint: gateway.value.trim(), acl: document.getElementById("bucket-policy-acl").value, policy: action.value === "policy_put" ? buildPolicy() : null};
  }
  function endpoint(kind) { return "/api/object-storage/buckets/policy-acl/" + kind + "?cluster=" + encodeURIComponent(form.dataset.cluster); }
  function parse(response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Thao tác thất bại"); }); }
  [action, bucket, owner, gateway, document.getElementById("bucket-policy-effect"), document.getElementById("bucket-policy-principal"), document.getElementById("bucket-policy-s3-action"), document.getElementById("bucket-policy-resource")].forEach(function (control) { control.addEventListener(control.tagName === "INPUT" ? "input" : "change", refresh); });
  form.addEventListener("submit", function (event) {
    event.preventDefault(); execute.disabled = true; preview.hidden = true; status.textContent = "Đang validate policy và đọc cấu hình hiện tại...";
    var body;
    try { body = payload(); } catch (error) { status.textContent = "Lỗi cấu hình: " + error.message; return; }
    fetch(endpoint("preview"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(parse).then(function (data) { approved = body; approved.expected = data.confirmation_required; diff.textContent = "Ceph " + data.ceph_version + " (" + data.ceph_release + ") · rủi ro " + data.risk + "\n\nTRƯỚC:\n" + JSON.stringify({policy: data.diff.before_policy, acl: data.diff.before_acl}, null, 2) + "\n\nSAU:\n" + JSON.stringify(data.diff.after, null, 2); warning.textContent = data.warning || "Không phát hiện public Principal/ACL."; confirmation.value = ""; preview.hidden = false; status.textContent = "Nhập " + data.confirmation_required + " để xác nhận."; })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; });
  });
  confirmation.addEventListener("input", function () { execute.disabled = !approved || confirmation.value !== approved.expected; });
  execute.addEventListener("click", function () {
    if (!approved) return; execute.disabled = true; status.textContent = "Đang áp dụng Bucket Policy/ACL...";
    var body = Object.assign({}, approved, {confirmation: confirmation.value}); delete body.expected;
    fetch(endpoint("execute"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(parse).then(function (data) { status.textContent = "Thành công. Request ID: " + data.request_id; approved = null; preview.hidden = true; })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; execute.disabled = false; });
  });
  refresh();
})();

(function () {
  var form = document.getElementById("bucket-create-form");
  if (!form) return;
  var preview = document.getElementById("bucket-create-preview");
  var summary = document.getElementById("bucket-create-summary");
  var confirmation = document.getElementById("bucket-create-confirmation");
  var execute = document.getElementById("bucket-create-execute");
  var status = document.getElementById("bucket-create-status");
  var objectLockInput = document.getElementById("bucket-create-object-lock");
  var objectLockSetting = document.getElementById("bucket-create-object-lock-label");
  var objectLockState = document.getElementById("bucket-create-object-lock-hint");
  var approved = null;
  function objectLockEnabled() { return objectLockInput.value.trim() === "OK"; }
  function updateObjectLockState() {
    var enabled = objectLockEnabled();
    objectLockSetting.classList.toggle("is-enabled", enabled);
    objectLockState.textContent = enabled ? "Object Lock sẽ được bật" : (objectLockInput.value.trim() ? "Cần nhập chính xác OK" : "Chưa bật");
  }
  objectLockInput.addEventListener("input", updateObjectLockState);
  updateObjectLockState();
  function payload() { return {
    name: document.getElementById("bucket-create-name").value.trim(),
    owner: document.getElementById("bucket-create-owner").value.trim(),
    endpoint: document.getElementById("bucket-create-endpoint").value.trim(),
    api_name: document.getElementById("bucket-create-api-name").value.trim(),
    placement: document.getElementById("bucket-create-placement").value.trim(),
    object_lock: objectLockEnabled()
  }; }
  function endpoint(kind) { return "/api/object-storage/buckets/actions/" + kind + "?cluster=" + encodeURIComponent(form.dataset.cluster); }
  function parse(response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Thao tác thất bại"); }); }
  form.addEventListener("submit", function (event) {
    event.preventDefault(); approved = null; preview.hidden = true; execute.disabled = true; status.textContent = "Đang kiểm tra phiên bản Ceph và owner...";
    var body = payload();
    fetch(endpoint("preview"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(parse).then(function (data) { approved = body; summary.textContent = data.preview + " · Ceph " + data.ceph_version + " (" + data.ceph_release + ") · rủi ro " + data.risk + ". " + data.temporary_key; confirmation.value = ""; preview.hidden = false; status.textContent = "Kiểm tra preview rồi nhập lại tên bucket."; })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; });
  });
  confirmation.addEventListener("input", function () { execute.disabled = !approved || confirmation.value !== approved.name; });
  execute.addEventListener("click", function () {
    if (!approved) return; execute.disabled = true; status.textContent = "Đang tạo bucket qua S3 API...";
    var body = Object.assign({}, approved, {confirmation: confirmation.value});
    fetch(endpoint("execute"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(parse).then(function (data) { status.textContent = "Đã tạo bucket. Request ID: " + data.request_id + ". Đang tải lại..."; window.location.reload(); })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; execute.disabled = false; });
  });
})();

(function () {
  var form = document.getElementById("bucket-lifecycle-form");
  if (!form) return;
  var action = document.getElementById("bucket-lifecycle-action");
  var rulesLabel = document.getElementById("bucket-lifecycle-rules-label");
  var ruleList = document.getElementById("bucket-lifecycle-rule-list");
  var generated = document.getElementById("bucket-lifecycle-generated-json");
  var bucket = document.getElementById("bucket-lifecycle-name");
  var owner = document.getElementById("bucket-lifecycle-owner");
  var gateway = document.getElementById("bucket-lifecycle-endpoint");
  var submit = document.getElementById("bucket-lifecycle-submit");
  var preview = document.getElementById("bucket-lifecycle-preview");
  var summary = document.getElementById("bucket-lifecycle-summary");
  var confirmation = document.getElementById("bucket-lifecycle-confirmation");
  var execute = document.getElementById("bucket-lifecycle-execute");
  var status = document.getElementById("bucket-lifecycle-status");
  var approved = null;
  function baseReady() { return Boolean(action.value && bucket.value.trim() && owner.value.trim() && gateway.value.trim() && gateway.checkValidity()); }
  function buildRules() {
    return Array.prototype.slice.call(ruleList.querySelectorAll(".bucket-lifecycle-rule")).map(function (row) {
      var type = row.querySelector("[data-rule-type]").value;
      var rule = {id: row.querySelector("[data-rule-id]").value.trim(), prefix: row.querySelector("[data-rule-prefix]").value, status: row.querySelector("[data-rule-status]").value};
      rule[type] = Number(row.querySelector("[data-rule-days]").value);
      if (type === "transition_days") rule.storage_class = row.querySelector("[data-rule-storage]").value;
      return rule;
    });
  }
  function refreshBuilder() {
    var ready = baseReady();
    rulesLabel.hidden = !ready || action.value !== "lifecycle_put";
    rulesLabel.querySelectorAll("input, select, button").forEach(function (control) { control.disabled = rulesLabel.hidden; });
    submit.hidden = !ready;
    if (!rulesLabel.hidden) generated.textContent = JSON.stringify(buildRules(), null, 2);
    preview.hidden = true; approved = null;
  }
  function addRule() {
    var row = document.createElement("div");
    row.className = "bucket-lifecycle-rule bucket-rule-grid";
    row.innerHTML = '<label>Rule ID<input data-rule-id maxlength="255" required placeholder="expire-logs"></label><label>Prefix<input data-rule-prefix maxlength="1024" placeholder="logs/"></label><label>Trạng thái<select data-rule-status><option value="Enabled">Enabled</option><option value="Disabled">Disabled</option></select></label><label>Hành động<select data-rule-type><option value="expiration_days">Expire object</option><option value="noncurrent_expiration_days">Expire noncurrent version</option><option value="abort_multipart_days">Abort multipart upload</option><option value="transition_days" data-capability="lifecycle-transition">Chuyển storage class</option></select></label><label>Số ngày<input data-rule-days type="number" min="1" max="36500" value="30" required></label><label data-storage-label hidden>Storage class<select data-rule-storage><option value="STANDARD_IA">STANDARD_IA</option><option value="ONEZONE_IA">ONEZONE_IA</option><option value="INTELLIGENT_TIERING">INTELLIGENT_TIERING</option><option value="REDUCED_REDUNDANCY">REDUCED_REDUNDANCY</option></select></label><button type="button" class="btn btn-ghost btn-sm" data-remove-rule>Xóa rule</button>';
    ruleList.appendChild(row);
    row.addEventListener("input", refreshBuilder);
    row.addEventListener("change", function (event) { row.querySelector("[data-storage-label]").hidden = row.querySelector("[data-rule-type]").value !== "transition_days"; refreshBuilder(); });
    row.querySelector("[data-remove-rule]").addEventListener("click", function () { if (ruleList.children.length > 1) { row.remove(); refreshBuilder(); } });
    refreshBuilder();
  }
  function payload() {
    return {action: action.value, bucket: bucket.value.trim(), owner: owner.value.trim(), endpoint: gateway.value.trim(), rules: action.value === "lifecycle_put" ? buildRules() : []};
  }
  function endpoint(kind) { return "/api/object-storage/buckets/lifecycle/" + kind + "?cluster=" + encodeURIComponent(form.dataset.cluster); }
  function parse(response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Thao tác thất bại"); }); }
  [action, bucket, owner, gateway].forEach(function (control) { control.addEventListener(control.tagName === "INPUT" ? "input" : "change", refreshBuilder); });
  document.getElementById("bucket-lifecycle-add-rule").addEventListener("click", addRule);
  addRule();
  form.addEventListener("submit", function (event) {
    event.preventDefault(); approved = null; preview.hidden = true; execute.disabled = true; status.textContent = "Đang validate và quét mẫu object...";
    var body;
    try { body = payload(); } catch (error) { status.textContent = "Lỗi cấu hình: " + error.message; return; }
    fetch(endpoint("preview"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(parse).then(function (data) { approved = body; var scan = data.dry_run; summary.textContent = "Ceph " + data.ceph_version + " (" + data.ceph_release + ") · rủi ro " + data.risk + "\nĐã quét: " + scan.scanned_objects + (scan.truncated ? " (bị giới hạn)" : "") + "\nƯớc lượng object hiện tại bị tác động: " + scan.estimated_current_objects_affected + "\nMultipart/noncurrent: " + scan.multipart_and_noncurrent_estimate + "\nRules mới:\n" + JSON.stringify(data.rules, null, 2); confirmation.value = ""; preview.hidden = false; status.textContent = "Kiểm tra dry-run rồi nhập lại tên bucket."; })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; });
  });
  confirmation.addEventListener("input", function () { execute.disabled = !approved || confirmation.value !== approved.bucket; });
  execute.addEventListener("click", function () {
    if (!approved) return; execute.disabled = true; status.textContent = "Đang áp dụng lifecycle...";
    fetch(endpoint("execute"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(Object.assign({}, approved, {confirmation: confirmation.value}))})
      .then(parse).then(function (data) { status.textContent = "Thành công. Request ID: " + data.request_id; approved = null; preview.hidden = true; })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; execute.disabled = false; });
  });
})();

(function () {
  var form = document.getElementById("bucket-governance-form");
  if (!form) return;
  var action = document.getElementById("bucket-governance-action");
  var preview = document.getElementById("bucket-governance-preview");
  var summary = document.getElementById("bucket-governance-summary");
  var confirmation = document.getElementById("bucket-governance-confirmation");
  var execute = document.getElementById("bucket-governance-execute");
  var status = document.getElementById("bucket-governance-status");
  var approved = null;
  function isS3() { return action.value.indexOf("versioning_") === 0 || action.value === "retention_set"; }
  function refreshFields() {
    document.querySelectorAll("[data-governance-s3]").forEach(function (item) { item.hidden = !isS3(); });
    document.querySelectorAll("[data-governance-quota]").forEach(function (item) { item.hidden = action.value !== "quota_set"; });
    document.querySelectorAll("[data-governance-retention]").forEach(function (item) { item.hidden = action.value !== "retention_set"; });
  }
  function payload() { return {
    action: action.value,
    bucket: document.getElementById("bucket-governance-name").value.trim(),
    owner: document.getElementById("bucket-governance-owner").value.trim(),
    endpoint: document.getElementById("bucket-governance-endpoint").value.trim(),
    max_size_bytes: document.getElementById("bucket-governance-size").value,
    max_objects: document.getElementById("bucket-governance-objects").value,
    mode: document.getElementById("bucket-governance-mode").value,
    days: document.getElementById("bucket-governance-days").value
  }; }
  function endpoint(kind) { return "/api/object-storage/buckets/governance/" + kind + "?cluster=" + encodeURIComponent(form.dataset.cluster); }
  function parse(response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Thao tác thất bại"); }); }
  action.addEventListener("change", function () { approved = null; preview.hidden = true; refreshFields(); });
  form.addEventListener("submit", function (event) {
    event.preventDefault(); approved = null; preview.hidden = true; execute.disabled = true; status.textContent = "Đang kiểm tra capability và bucket...";
    var body = payload();
    fetch(endpoint("preview"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(parse).then(function (data) { approved = body; summary.textContent = data.preview + " · Ceph " + data.ceph_version + " (" + data.ceph_release + ") · rủi ro " + data.risk; confirmation.value = ""; preview.hidden = false; status.textContent = "Kiểm tra preview rồi nhập lại tên bucket."; })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; });
  });
  confirmation.addEventListener("input", function () { execute.disabled = !approved || confirmation.value !== approved.bucket; });
  execute.addEventListener("click", function () {
    if (!approved) return; execute.disabled = true; status.textContent = "Đang thực thi...";
    fetch(endpoint("execute"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(Object.assign({}, approved, {confirmation: confirmation.value}))})
      .then(parse).then(function (data) { status.textContent = "Thành công. Request ID: " + data.request_id; approved = null; preview.hidden = true; })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; execute.disabled = false; });
  });
  refreshFields();
})();
