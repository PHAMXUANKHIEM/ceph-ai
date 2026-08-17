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
        document.getElementById("bucket-lifecycle-status").textContent = lifecycle.transition_unavailable_reason + " Các rule expiration vẫn được phép.";
      }
      if (!data.bucket_policy_acl.supported) {
        disableForm("bucket-policy-form", data.bucket_policy_acl.unavailable_reason);
        document.getElementById("bucket-policy-status").textContent = data.bucket_policy_acl.unavailable_reason;
      }
    })
    .catch(function (error) {
      capabilityStatus.textContent = "Không xác định được phiên bản/capability Ceph: " + error.message + ". Các thao tác ghi đã bị khóa.";
      ["bucket-create-form", "bucket-governance-form", "bucket-lifecycle-form", "bucket-policy-form"].forEach(function (id) { disableForm(id, error.message); });
    });
})();

(function () {
  var form = document.getElementById("bucket-policy-form");
  if (!form) return;
  var action = document.getElementById("bucket-policy-action");
  var policyLabel = document.getElementById("bucket-policy-json-label");
  var aclLabel = document.getElementById("bucket-policy-acl-label");
  var preview = document.getElementById("bucket-policy-preview");
  var diff = document.getElementById("bucket-policy-diff");
  var warning = document.getElementById("bucket-policy-warning");
  var confirmation = document.getElementById("bucket-policy-confirmation");
  var execute = document.getElementById("bucket-policy-execute");
  var status = document.getElementById("bucket-policy-status");
  var approved = null;
  function refresh() { policyLabel.hidden = action.value !== "policy_put"; aclLabel.hidden = action.value !== "acl_set"; preview.hidden = true; approved = null; }
  function payload() {
    var policy = null;
    if (action.value === "policy_put") policy = JSON.parse(document.getElementById("bucket-policy-json").value);
    return {action: action.value, bucket: document.getElementById("bucket-policy-name").value.trim(), owner: document.getElementById("bucket-policy-owner").value.trim(), endpoint: document.getElementById("bucket-policy-endpoint").value.trim(), acl: document.getElementById("bucket-policy-acl").value, policy: policy};
  }
  function endpoint(kind) { return "/api/object-storage/buckets/policy-acl/" + kind + "?cluster=" + encodeURIComponent(form.dataset.cluster); }
  function parse(response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Thao tác thất bại"); }); }
  action.addEventListener("change", refresh);
  form.addEventListener("submit", function (event) {
    event.preventDefault(); execute.disabled = true; preview.hidden = true; status.textContent = "Đang validate policy và đọc cấu hình hiện tại...";
    var body;
    try { body = payload(); } catch (error) { status.textContent = "Lỗi JSON: " + error.message; return; }
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
  var approved = null;
  function payload() { return {
    name: document.getElementById("bucket-create-name").value.trim(),
    owner: document.getElementById("bucket-create-owner").value.trim(),
    endpoint: document.getElementById("bucket-create-endpoint").value.trim(),
    api_name: document.getElementById("bucket-create-api-name").value.trim(),
    placement: document.getElementById("bucket-create-placement").value.trim(),
    object_lock: document.getElementById("bucket-create-object-lock").checked
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
  var rules = document.getElementById("bucket-lifecycle-rules");
  var rulesLabel = document.getElementById("bucket-lifecycle-rules-label");
  var preview = document.getElementById("bucket-lifecycle-preview");
  var summary = document.getElementById("bucket-lifecycle-summary");
  var confirmation = document.getElementById("bucket-lifecycle-confirmation");
  var execute = document.getElementById("bucket-lifecycle-execute");
  var status = document.getElementById("bucket-lifecycle-status");
  var approved = null;
  function payload() {
    var parsed = [];
    if (action.value === "lifecycle_put") parsed = JSON.parse(rules.value);
    return {action: action.value, bucket: document.getElementById("bucket-lifecycle-name").value.trim(), owner: document.getElementById("bucket-lifecycle-owner").value.trim(), endpoint: document.getElementById("bucket-lifecycle-endpoint").value.trim(), rules: parsed};
  }
  function endpoint(kind) { return "/api/object-storage/buckets/lifecycle/" + kind + "?cluster=" + encodeURIComponent(form.dataset.cluster); }
  function parse(response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Thao tác thất bại"); }); }
  action.addEventListener("change", function () { rulesLabel.hidden = action.value === "lifecycle_delete"; approved = null; preview.hidden = true; });
  form.addEventListener("submit", function (event) {
    event.preventDefault(); approved = null; preview.hidden = true; execute.disabled = true; status.textContent = "Đang validate và quét mẫu object...";
    var body;
    try { body = payload(); } catch (error) { status.textContent = "Lỗi JSON: " + error.message; return; }
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
