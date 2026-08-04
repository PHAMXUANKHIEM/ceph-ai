(function () {
  var form = document.getElementById("bluestore-quick-fix-form");
  if (!form) {
    return; // no OSDs listed, or not on this page
  }

  var osdSelect = document.getElementById("bqf-osd-id");
  var hostSelect = document.getElementById("bqf-host");
  var statusEl = document.getElementById("bqf-status");

  function handleAuthRedirect(response) {
    if (response.redirected && response.url.indexOf("/login") !== -1) {
      window.location.reload();
      throw new Error("unauthenticated");
    }
    return response;
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    statusEl.textContent = "Đang tạo đề xuất...";

    fetch("/nodes/bluestore-quick-fix/propose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        osd_id: parseInt(osdSelect.value, 10),
        host: hostSelect.value,
      }),
    })
      .then(handleAuthRedirect)
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (body) {
            throw new Error((body && body.detail) || "Không tạo được đề xuất");
          });
        }
        return response.json();
      })
      .then(function () {
        statusEl.textContent =
          "Đã tạo đề xuất — vào Dashboard (trang chủ) để duyệt trước khi thực thi.";
      })
      .catch(function (err) {
        statusEl.textContent = "Lỗi: " + err.message;
      });
  });
})();
