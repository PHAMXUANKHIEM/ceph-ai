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
