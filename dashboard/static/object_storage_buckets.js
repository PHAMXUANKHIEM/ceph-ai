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
    placement: document.getElementById("bucket-create-placement").value.trim()
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
