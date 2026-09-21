function bucketHighlightJSON(value) {
  var raw = typeof value === "string" ? value : JSON.stringify(value, null, 2);
  var escaped = raw.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  return escaped.replace(/("(?:\\.|[^"\\])*"\s*:)|("(?:\\.|[^"\\])*")|\b(true|false|null)\b|\b(-?\d+(?:\.\d+)?)\b/g, function (match, key, string, literal, number) {
    if (key) return '<span class="json-key">' + key.slice(0, -1) + '</span>:';
    if (string) return '<span class="json-string">' + match + '</span>';
    if (literal) return '<span class="json-literal">' + match + '</span>';
    if (number) return '<span class="json-number">' + match + '</span>';
    return match;
  });
}

(function () {
  document.querySelectorAll(".bucket-created-time").forEach(function (element) {
    var date = new Date(element.getAttribute("datetime") || "");
    if (Number.isNaN(date.getTime())) return;
    element.textContent = new Intl.DateTimeFormat("vi-VN", {
      day: "2-digit", month: "2-digit", year: "numeric",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(date);
    element.title = element.getAttribute("datetime") || "";
  });
})();

(function () {
  var page = document.querySelector(".bucket-page");
  if (!page) return;
  var rows = Array.prototype.slice.call(document.querySelectorAll("tr.bucket-row-pending[data-bucket-name]"));
  if (!rows.length) return;
  var activeLoads = 0;
  var nextRow = 0;
  var MAX_ACTIVE_LOADS = 3;
  var MAX_PENDING_ATTEMPTS = 18;
  var RETRY_DELAY_MS = 1000;

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>\"']/g, function (character) {
      return {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[character];
    });
  }

  function formatCreatedTimes(root) {
    root.querySelectorAll(".bucket-created-time").forEach(function (element) {
      var date = new Date(element.getAttribute("datetime") || "");
      if (Number.isNaN(date.getTime())) return;
      element.textContent = new Intl.DateTimeFormat("vi-VN", {
        day: "2-digit", month: "2-digit", year: "numeric",
        hour: "2-digit", minute: "2-digit", hour12: false,
      }).format(date);
      element.title = element.getAttribute("datetime") || "";
    });
  }

  function formatBytes(value) {
    var size = Number(value);
    if (!Number.isFinite(size) || size < 0) return "—";
    var units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
    var index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return (index === 0 ? Math.round(size) : size.toFixed(1)) + " " + units[index];
  }
  function setField(row, field, html) {
    var target = row.querySelector('[data-bucket-field="' + field + '"]');
    if (target) target.innerHTML = html;
  }
  function setError(row, message) {
    setField(row, "owner", '<span class="muted-value">Không lấy được</span>');
    setField(row, "objects", '<span class="muted-value">—</span>');
    setField(row, "size", '<span class="muted-value" title="' + escapeHtml(message || "") + '">Không khả dụng</span>');
    setField(row, "quota", '<span class="muted-value">—</span>');
    setField(row, "created", '<span class="muted-value">—</span>');
    setField(row, "status", '<span class="status-badge warning">Chưa đầy đủ</span>');
    row.classList.remove("bucket-row-pending");
  }
  function apply(row, data) {
    if (!data || !data.ready) return false;
    if (data.error) { setError(row, data.error); return true; }
    var quota = data.quota_enabled
      ? escapeHtml(data.quota_size || "—") + " / " + escapeHtml(data.quota_max_objects == null ? "—" : data.quota_max_objects) + " objects"
      : '<span class="muted-value">Không giới hạn</span>';
    setField(row, "owner", escapeHtml(data.owner || "—"));
    setField(row, "objects", escapeHtml(data.num_objects == null ? "—" : data.num_objects));
    setField(row, "size", formatBytes(data.size_bytes) + (Number(data.num_objects || 0) === 0 && Number(data.size_bytes || 0) === 0 ? ' <span class="bucket-empty-badge">Trống</span>' : ""));
    setField(row, "quota", quota);
    setField(row, "created", data.creation_time ? '<time class="bucket-created-time" datetime="' + escapeHtml(data.creation_time) + '" title="' + escapeHtml(data.creation_time) + '">' + escapeHtml(data.creation_time) + '</time>' : "—");
    setField(row, "status", '<span class="status-badge healthy">Sẵn sàng</span>');
    formatCreatedTimes(row);
    row.classList.remove("bucket-row-pending");
    var manage = row.querySelector("[data-bucket-action=menu]");
    if (manage) {
      manage.dataset.bucketOwner = data.owner || "";
      manage.dataset.bucketSize = data.size || formatBytes(data.size_bytes);
      manage.dataset.bucketObjects = String(data.num_objects || 0);
    }
    return true;
  }
  function load(row, attempt, done) {
    var bucket = row.dataset.bucketName || "";
    var url = "/api/object-storage/buckets/" + encodeURIComponent(bucket) + "?cluster=" + encodeURIComponent(page.dataset.cluster || "");
    fetch(url, {cache: "no-store"}).then(function (response) {
      return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Không đọc được metadata"); });
    }).then(function (data) {
      var ready = apply(row, data);
      if (!ready && attempt < MAX_PENDING_ATTEMPTS) {
        window.setTimeout(function () { load(row, attempt + 1, done); }, RETRY_DELAY_MS);
      } else {
        if (!ready) setError(row, "RGW phản hồi chậm");
        done();
      }
    }).catch(function (error) {
      if (attempt < 2) window.setTimeout(function () { load(row, attempt + 1, done); }, RETRY_DELAY_MS);
      else { setError(row, error.message); done(); }
    });
  }
  function pump() {
    while (activeLoads < MAX_ACTIVE_LOADS && nextRow < rows.length) {
      var row = rows[nextRow++];
      activeLoads += 1;
      load(row, 0, function () { activeLoads -= 1; pump(); });
    }
  }
  pump();
})();

(function () {
  var banner = document.getElementById("bucket-capability-banner");
  var dismiss = banner && banner.querySelector(".bucket-capability-dismiss");
  if (!banner || !dismiss) return;
  var storageKey = "ceph-ai.bucket-capability-banner.dismissed";
  try { if (window.localStorage.getItem(storageKey) === "1") banner.hidden = true; } catch (_) {}
  dismiss.addEventListener("click", function () {
    banner.hidden = true;
    try { window.localStorage.setItem(storageKey, "1"); } catch (_) {}
  });
})();

(function () {
  document.querySelectorAll("[data-bucket-filter-select]").forEach(function (select) {
    var field = select.parentElement;
    var input = field.querySelector("input[type=hidden]");
    var label = select.querySelector("[data-filter-label]");
    if (!input || !label) return;
    select.querySelectorAll("[data-filter-value]").forEach(function (option) {
      option.addEventListener("click", function () {
        input.value = option.dataset.filterValue;
        label.textContent = option.textContent.trim();
        select.querySelectorAll("[data-filter-value]").forEach(function (item) {
          item.setAttribute("aria-selected", item === option ? "true" : "false");
        });
        select.removeAttribute("open");
      });
    });
  });
})();

(function () {
  var drawer = document.getElementById("bucket-action-drawer");
  var backdrop = document.getElementById("bucket-action-backdrop");
  if (!drawer || !backdrop) return;
  var title = document.getElementById("bucket-drawer-title");
  var context = document.getElementById("bucket-drawer-context");
  var nav = document.getElementById("bucket-action-nav");
  var previousFocus = null;
  var labels = {governance: "Quota & Retention", "user-settings": "User Quota & Capability", lifecycle: "Lifecycle Policy", policy: "Bucket Policy & ACL", delete: "Xóa bucket"};
  var page = document.querySelector(".bucket-page");
  function configuredEndpoint() { return page.dataset.rgwEndpoint || (page.dataset.rgwHost ? "http://" + page.dataset.rgwHost + ":7480" : ""); }

  function configuredEndpoints() {
    var values = (page.dataset.rgwEndpoints || "").split(",").map(function (value) { return value.trim(); }).filter(Boolean);
    var current = configuredEndpoint();
    if (current && values.indexOf(current) < 0) values.unshift(current);
    return values.filter(function (value, index) { return values.indexOf(value) === index; });
  }
  function enableEndpointInputs() {
    var endpoints = configuredEndpoints();
    var apiName = document.getElementById("bucket-create-api-name");
    if (apiName) apiName.readOnly = false;
    ["bucket-create-endpoint", "bucket-governance-endpoint", "bucket-lifecycle-endpoint", "bucket-policy-endpoint", "bucket-delete-endpoint"].forEach(function (id) {
      var input = document.getElementById(id);
      if (!input) return;
      input.readOnly = false;
      if (!endpoints.length) return;
      input.setAttribute("list", id + "-options");
      var list = document.createElement("datalist");
      list.id = id + "-options";
      endpoints.forEach(function (endpoint) {
        var option = document.createElement("option");
        option.value = endpoint;
        list.appendChild(option);
      });
      input.parentNode.appendChild(list);
    });
  }

  function value(id, next) {
    var control = document.getElementById(id);
    if (control) control.value = next || "";
  }
  function fillBucket(data) {
    ["governance", "lifecycle", "policy", "delete"].forEach(function (kind) {
      value("bucket-" + kind + "-name", data.name);
      value("bucket-" + kind + "-owner", data.owner);
      value("bucket-" + kind + "-endpoint", configuredEndpoint());
    });
    value("s3-setting-uid", data.owner);
    value("s3-setting-quota-scope", "bucket");
    var deleteAction = document.getElementById("bucket-delete-action");
    if (deleteAction) deleteAction.value = Number(data.objects || 0) > 0 ? "purge_delete" : "delete_empty";
  }
  function showPanel(kind) {
    document.querySelectorAll("[data-action-panel]").forEach(function (panel) { panel.hidden = panel.dataset.actionPanel !== kind; });
    document.querySelectorAll("[data-drawer-action]").forEach(function (button) { button.classList.toggle("is-active", button.dataset.drawerAction === kind); });
    title.textContent = labels[kind] || "Bucket action";
    nav.hidden = kind === "create";
    if (kind === "governance") document.getElementById("bucket-governance-action").dispatchEvent(new Event("change"));
    if (kind === "lifecycle") document.getElementById("bucket-lifecycle-action").dispatchEvent(new Event("change"));
    if (kind === "policy") document.getElementById("bucket-policy-action").dispatchEvent(new Event("change"));
  }
  function open(kind, data) {
    previousFocus = document.activeElement;
    if (data) {
      fillBucket(data);
      context.textContent = data.name + " · owner " + (data.owner || "chưa có metadata");
    } else {
      context.textContent = "Cấu hình được áp dụng cho cụm đang chọn.";
    }
    value("bucket-create-endpoint", configuredEndpoint());
    backdrop.hidden = false; drawer.hidden = false; drawer.setAttribute("aria-hidden", "false");
    document.body.classList.add("bucket-drawer-open");
    window.dispatchEvent(new Event("bucket-drawer-context"));
    showPanel(kind);
    var focusTarget = kind === "create" ? document.getElementById("bucket-create-name") : drawer.querySelector("[data-action-panel='" + kind + "'] input");
    if (focusTarget) window.setTimeout(function () { focusTarget.focus(); }, 0);
  }
  function close() {
    backdrop.hidden = true; drawer.hidden = true; drawer.setAttribute("aria-hidden", "true"); document.body.classList.remove("bucket-drawer-open");
    if (previousFocus && previousFocus.focus) previousFocus.focus();
  }
  enableEndpointInputs();
  document.querySelectorAll("[data-bucket-action]").forEach(function (button) {
    button.addEventListener("click", function () {
      if (button.dataset.bucketAction === "create") return open("create");
      if (button.dataset.bucketAction === "menu") return open("governance", {name: button.dataset.bucketName, owner: button.dataset.bucketOwner, objects: button.dataset.bucketObjects});
    });
  });
  nav.querySelectorAll("[data-drawer-action]").forEach(function (button) { button.addEventListener("click", function () { showPanel(button.dataset.drawerAction); }); });
  document.querySelectorAll("[data-preview-form]").forEach(function (button) { button.addEventListener("click", function () { var form = document.getElementById(button.dataset.previewForm); if (form.requestSubmit) form.requestSubmit(); else form.dispatchEvent(new Event("submit", {cancelable: true})); }); });
  document.querySelectorAll("[data-close-bucket-drawer]").forEach(function (button) { button.addEventListener("click", close); });
  backdrop.addEventListener("click", close);
  document.addEventListener("keydown", function (event) { if (event.key === "Escape" && !drawer.hidden) close(); });
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
  function setTitle(id, title) {
    var element = document.getElementById(id);
    if (element) element.title = title || "";
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
        setTitle("bucket-create-placement-label", create.placement_unavailable_reason);
        setTitle("bucket-create-api-name-label", create.placement_unavailable_reason);
      }
      if (!governance.object_lock_at_create) {
        document.getElementById("bucket-create-object-lock").disabled = true;
        setTitle("bucket-create-object-lock-label", governance.object_lock_unavailable_reason);
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
  var page = document.querySelector(".bucket-page");
  var loading = document.querySelector(".bucket-load-state");
  if (!page || !loading || loading.textContent.indexOf("Đang tải") === -1) return;

  var attempts = 0;
  var maxAttempts = 8;
  function retryDelay() { return Math.min(10000, 1500 * attempts); }
  function checkInventory() {
    attempts += 1;
    var params = new URLSearchParams(window.location.search);
    params.set("cluster", page.dataset.cluster || "");
    fetch("/api/object-storage/buckets?" + params.toString(), {cache: "no-store"})
      .then(function (response) { return response.ok ? response.json() : null; })
      .then(function (data) {
        if (data && !data.refreshing) {
          if (data.refresh_error) {
            loading.textContent = "Không thể đồng bộ inventory bucket từ RGW. Bấm tải lại để thử lại.";
            return;
          }
          window.location.reload();
          return;
        }
        if (attempts < maxAttempts) window.setTimeout(checkInventory, retryDelay());
        else loading.textContent = "RGW đang phản hồi chậm. Bấm tải lại để kiểm tra lại danh sách bucket.";
      })
      .catch(function () {
        if (attempts < maxAttempts) window.setTimeout(checkInventory, retryDelay());
        else loading.textContent = "Không kiểm tra được trạng thái RGW. Bấm tải lại để thử lại.";
      });
  }
  window.setTimeout(checkInventory, 1500);
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
  window.addEventListener("bucket-drawer-context", function () { approved = null; preview.hidden = true; confirmation.value = ""; status.textContent = ""; });
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
  window.addEventListener("bucket-drawer-context", function () { approved = null; preview.hidden = true; confirmation.value = ""; status.textContent = ""; });
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
    if (baseReady && action.value === "policy_put") generated.innerHTML = bucketHighlightJSON(JSON.stringify(buildPolicy(), null, 2));
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
  var approved = null;
  window.addEventListener("bucket-drawer-context", function () { approved = null; preview.hidden = true; confirmation.value = ""; status.textContent = ""; });
  function payload() { return {
    name: document.getElementById("bucket-create-name").value.trim(),
    owner: document.getElementById("bucket-create-owner").value.trim(),
    endpoint: document.getElementById("bucket-create-endpoint").value.trim(),
    api_name: document.getElementById("bucket-create-placement").value.trim() ? document.getElementById("bucket-create-api-name").value.trim() : "",
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
  window.addEventListener("bucket-drawer-context", function () { approved = null; preview.hidden = true; confirmation.value = ""; status.textContent = ""; });
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
    if (!rulesLabel.hidden) generated.innerHTML = bucketHighlightJSON(JSON.stringify(buildRules(), null, 2));
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
  var unlimitedSize = document.getElementById("bucket-governance-unlimited-size");
  var unlimitedObjects = document.getElementById("bucket-governance-unlimited-objects");
  var sizeInput = document.getElementById("bucket-governance-size");
  var objectsInput = document.getElementById("bucket-governance-objects");
  var approved = null;
  window.addEventListener("bucket-drawer-context", function () { approved = null; preview.hidden = true; confirmation.value = ""; status.textContent = ""; });
  function isS3() { return action.value.indexOf("versioning_") === 0 || action.value === "retention_set"; }
  function refreshFields() {
    document.querySelectorAll("[data-governance-s3]").forEach(function (item) { item.hidden = !isS3(); });
    document.querySelectorAll("[data-governance-quota]").forEach(function (item) { item.hidden = action.value !== "quota_set"; });
    document.querySelectorAll("[data-governance-retention]").forEach(function (item) { item.hidden = action.value !== "retention_set"; });
    sizeInput.disabled = unlimitedSize.checked || action.value !== "quota_set";
    objectsInput.disabled = unlimitedObjects.checked || action.value !== "quota_set";
    sizeInput.required = !unlimitedSize.checked && action.value === "quota_set";
    objectsInput.required = !unlimitedObjects.checked && action.value === "quota_set";
    document.getElementById("governance-action-help").textContent = {
      quota_set: "Nhập một hoặc cả hai giới hạn; bật “Không giới hạn” cho phần không cần giới hạn.",
      quota_enable: "Bật quota hiện tại của bucket.", quota_disable: "Tắt quota; dữ liệu hiện tại không bị xóa.",
      versioning_enable: "Bật lưu version cho object mới.", versioning_suspend: "Tạm dừng tạo version mới; version cũ vẫn giữ nguyên.",
      retention_set: "Đặt thời hạn mặc định cho object mới trong bucket có Object Lock."
    }[action.value] || "Chọn thao tác cần áp dụng cho bucket này.";
  }
  function payload() { return {
    action: action.value,
    bucket: document.getElementById("bucket-governance-name").value.trim(),
    owner: document.getElementById("bucket-governance-owner").value.trim(),
    endpoint: document.getElementById("bucket-governance-endpoint").value.trim(),
    max_size_bytes: unlimitedSize.checked ? "-1" : sizeInput.value,
    max_objects: unlimitedObjects.checked ? "-1" : objectsInput.value,
    mode: document.getElementById("bucket-governance-mode").value,
    days: document.getElementById("bucket-governance-days").value
  }; }
  function endpoint(kind) { return "/api/object-storage/buckets/governance/" + kind + "?cluster=" + encodeURIComponent(form.dataset.cluster); }
  function parse(response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Thao tác thất bại"); }); }
  action.addEventListener("change", function () { approved = null; preview.hidden = true; refreshFields(); });
  unlimitedSize.addEventListener("change", refreshFields);
  unlimitedObjects.addEventListener("change", refreshFields);
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

(function () {
  var panel = document.getElementById("rgw-observability");
  if (!panel) return;
  var status = document.getElementById("rgw-observability-status");
  var refresh = document.getElementById("rgw-observability-refresh");

  function formatBytes(value) {
    var size = Number(value);
    if (!Number.isFinite(size) || size < 0) return "—";
    var units = ["B", "KiB", "MiB", "GiB", "TiB"];
    var index = 0;
    while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
    return (index === 0 ? Math.round(size) : size.toFixed(1)) + " " + units[index];
  }

  function renderTop(id, values) {
    var target = document.getElementById(id);
    target.replaceChildren();
    var entries = Object.entries(values || {}).slice(0, 5);
    if (!entries.length) {
      var empty = document.createElement("span"); empty.className = "muted-value"; empty.textContent = "Chưa có dữ liệu";
      target.appendChild(empty); return;
    }
    var maximum = Math.max.apply(null, entries.map(function (entry) { return Number(entry[1]) || 0; }));
    entries.forEach(function (entry) {
      var row = document.createElement("div"); row.className = "rgw-top-row";
      var label = document.createElement("span"); label.textContent = entry[0] || "—";
      var count = document.createElement("strong"); count.textContent = String(entry[1]);
      var meter = document.createElement("i"); meter.style.width = Math.max(4, ((Number(entry[1]) || 0) / Math.max(1, maximum)) * 100) + "%";
      row.append(label, count); row.appendChild(meter); target.appendChild(row);
    });
  }

  function load() {
    refresh.disabled = true;
    status.textContent = "Đang tải RGW metrics…";
    fetch("/api/object-storage/rgw-metrics?cluster=" + encodeURIComponent(panel.dataset.cluster), {cache: "no-store"})
      .then(function (response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Không đọc được RGW metrics"); }); })
      .then(function (body) {
        var metrics = body.metrics || {};
        document.getElementById("rgw-metric-requests").textContent = Number(metrics.request_count || 0).toLocaleString("vi-VN");
        document.getElementById("rgw-metric-bytes").textContent = formatBytes(metrics.bytes_total);
        document.getElementById("rgw-metric-errors").textContent = Number(metrics.error_rate_percent || 0).toFixed(2) + "%";
        document.getElementById("rgw-metric-latency").textContent = metrics.latency_p95_ms == null ? "—" : Number(metrics.latency_p95_ms).toFixed(1) + " ms";
        renderTop("rgw-top-buckets", metrics.top_buckets);
        renderTop("rgw-top-requesters", metrics.top_requesters);
        var collection = body.collection || {};
        var windowText = metrics.time_start && metrics.time_end ? " · " + metrics.time_start + " → " + metrics.time_end : "";
        status.textContent = "Nguồn " + (body.source || "RGW") + " · " + (collection.status || body.status || "unknown") + windowText;
        if ((body.evidence_gaps || []).length) status.textContent += " · " + body.evidence_gaps[0];
      })
      .catch(function (error) { status.textContent = "Không tải được RGW metrics: " + error.message; })
      .finally(function () { refresh.disabled = false; });
  }

  refresh.addEventListener("click", load);
  load();
})();

(function () {
  var panel = document.getElementById("rgw-health");
  if (!panel) return;
  var status = document.getElementById("rgw-health-status");
  var refresh = document.getElementById("rgw-health-refresh");
  function text(id, value) { document.getElementById(id).textContent = value == null || value === "" ? "—" : String(value); }
  function details(section) { return section && section.details && typeof section.details === "object" ? section.details : {}; }
  function identity(section) {
    var value = details(section);
    return value.name || value.id || value.epoch || value.status || (section && section.status) || "—";
  }
  function state(section) {
    var value = details(section);
    return value.sync_status || value.status || (section && section.status) || "—";
  }
  function load() {
    refresh.disabled = true;
    status.textContent = "Đang tải RGW health và topology…";
    var query = "?cluster=" + encodeURIComponent(panel.dataset.cluster);
    Promise.all([
      fetch("/api/object-storage/rgw-evidence" + query, {cache: "no-store"}).then(function (response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Không đọc được RGW evidence"); }); }),
      fetch("/api/object-storage/multisite-diagnosis" + query, {cache: "no-store"}).then(function (response) { return response.ok ? response.json() : response.json().then(function (body) { throw new Error(body.detail || "Không đọc được multisite diagnosis"); }); })
    ]).then(function (results) {
      var evidence = results[0] || {};
      var diagnosis = results[1] || {};
      var daemons = evidence.daemons || {};
      var endpoints = evidence.endpoints || {};
      var frontend = evidence.frontend || {};
      var sync = evidence.sync || {};
      text("rgw-health-daemons", (daemons.items || []).length + " · " + (daemons.status || "—"));
      text("rgw-health-endpoints", (endpoints.items || []).join(", ") || endpoints.status || "—");
      text("rgw-health-frontend", (frontend.items || []).map(function (item) { return item.key + "=" + item.value; }).join(", ") || frontend.status || "—");
      text("rgw-health-sync", state(sync));
      text("rgw-health-realm", identity((evidence.topology || {}).realm));
      text("rgw-health-zonegroup", identity((evidence.topology || {}).zonegroup));
      text("rgw-health-zone", identity((evidence.topology || {}).zone));
      text("rgw-health-period", identity(evidence.period));
      var lag = diagnosis.observed && diagnosis.observed.lag ? diagnosis.observed.lag.seconds : null;
      var findings = (diagnosis.findings || []).length;
      text("rgw-health-diagnosis", (diagnosis.status || "—") + " · " + findings + " finding(s)" + (lag == null ? "" : " · lag " + lag + "s"));
      var capacity = evidence.capacity || {};
      text("rgw-health-capacity", "Capacity dependency: " + ((capacity.items || []).length ? (capacity.items || []).length + " placement pool(s) mapped" : (capacity.status || "not available")));
      var gaps = (evidence.evidence_gaps || []).concat(diagnosis.evidence_gaps || []);
      var findingsList = document.getElementById("rgw-health-findings");
      findingsList.replaceChildren();
      var messages = (diagnosis.findings || []).slice(0, 5).map(function (item) {
        return (item.severity || "info").toUpperCase() + ": " + (item.summary || item.code || "Finding");
      }).concat(gaps.slice(0, 3).map(function (gap) { return "GAP: " + gap; }));
      if (!messages.length) messages.push("Không có finding hoặc evidence gap trong snapshot hiện tại.");
      messages.forEach(function (message) { var li = document.createElement("li"); li.textContent = message; findingsList.appendChild(li); });
      status.textContent = "Evidence " + (evidence.status || "unknown") + " · diagnosis " + (diagnosis.status || "unknown") + (gaps.length ? " · " + gaps[0] : "");
    }).catch(function (error) { status.textContent = "Không tải được RGW health: " + error.message; })
      .finally(function () { refresh.disabled = false; });
  }
  refresh.addEventListener("click", load);
  load();
})();
