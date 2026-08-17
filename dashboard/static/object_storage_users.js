(function () {
  var form = document.getElementById("s3-user-action-form");
  if (!form) return;
  var action = document.getElementById("s3-action");
  var uid = document.getElementById("s3-uid");
  var displayName = document.getElementById("s3-display-name");
  var email = document.getElementById("s3-email");
  var preview = document.getElementById("s3-preview");
  var summary = document.getElementById("s3-preview-summary");
  var command = document.getElementById("s3-preview-command");
  var confirmation = document.getElementById("s3-confirmation");
  var execute = document.getElementById("s3-execute");
  var status = document.getElementById("s3-action-status");
  var approvedPayload = null;
  function payload() { return {action: action.value, uid: uid.value.trim(), display_name: displayName.value.trim(), email: email.value.trim()}; }
  function endpoint(kind) { return "/api/object-storage/users/actions/" + kind + "?cluster=" + encodeURIComponent(form.dataset.cluster); }
  function errorText(response) { return response.json().then(function (body) { throw new Error(body.detail || "Thao tác thất bại"); }); }
  form.addEventListener("submit", function (event) {
    event.preventDefault(); approvedPayload = null; execute.disabled = true; preview.hidden = true; status.textContent = "Đang tạo preview...";
    fetch(endpoint("preview"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(payload())})
      .then(function (response) { return response.ok ? response.json() : errorText(response); })
      .then(function (data) { approvedPayload = payload(); summary.textContent = data.action + " S3 user " + data.uid + " trên cluster " + data.cluster_name + " · rủi ro " + data.risk; command.textContent = data.preview; confirmation.value = ""; preview.hidden = false; status.textContent = "Kiểm tra preview rồi nhập lại UID."; })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; });
  });
  confirmation.addEventListener("input", function () { execute.disabled = !approvedPayload || confirmation.value !== approvedPayload.uid; });
  execute.addEventListener("click", function () {
    if (!approvedPayload) return; execute.disabled = true; status.textContent = "Đang thực thi...";
    var body = Object.assign({}, approvedPayload, {confirmation: confirmation.value});
    fetch(endpoint("execute"), {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)})
      .then(function (response) { return response.ok ? response.json() : errorText(response); })
      .then(function () { status.textContent = "Thao tác thành công. Đang tải lại..."; window.location.reload(); })
      .catch(function (error) { status.textContent = "Lỗi: " + error.message; execute.disabled = false; });
  });
})();
