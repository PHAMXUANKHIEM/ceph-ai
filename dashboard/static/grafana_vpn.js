(function () {
  "use strict";
  var panel = document.querySelector('[data-panel="grafana-vpn"]');
  if (!panel) return;

  var state = document.getElementById("grafana-vpn-state");
  var message = document.getElementById("grafana-vpn-message");
  var form = document.getElementById("grafana-vpn-upload");
  var connect = document.getElementById("grafana-vpn-connect");
  var disconnect = document.getElementById("grafana-vpn-disconnect");
  var refresh = document.getElementById("grafana-vpn-refresh");
  var auth = document.getElementById("grafana-vpn-auth");
  var username = document.getElementById("grafana-vpn-username");
  var password = document.getElementById("grafana-vpn-password");

  function showMessage(value, error) {
    message.textContent = value || "";
    message.classList.toggle("is-error", !!error);
  }

  function render(data) {
    var labels = { connected: "Đã kết nối", connecting: "Đang kết nối", disconnected: "Chưa kết nối" };
    state.textContent = labels[data.state] || "Không rõ trạng thái";
    state.dataset.state = data.state || "unknown";
    document.getElementById("grafana-vpn-installed").textContent = data.installed ? "Đã cài" : "Chưa cài trên host";
    document.getElementById("grafana-vpn-configured").textContent = data.configured ? "Đã lưu an toàn" : "Chưa có profile";
    document.getElementById("grafana-vpn-interface").textContent = data.route_device || "—";
    auth.hidden = !data.auth_required;
    connect.disabled = !data.installed || !data.configured || data.state !== "disconnected";
    disconnect.disabled = data.state === "disconnected";
    form.querySelector('button[type="submit"]').disabled = data.state !== "disconnected";
  }

  async function request(url, options) {
    var response = await fetch(url, Object.assign({ credentials: "same-origin" }, options || {}));
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) throw new Error(data.detail || "Yêu cầu VPN thất bại.");
    return data;
  }

  async function loadStatus() {
    try { render(await request("/api/settings/grafana-vpn")); }
    catch (error) { showMessage(error.message, true); state.textContent = "Không truy cập được helper"; }
  }

  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    var file = document.getElementById("grafana-vpn-file").files[0];
    if (!file || !file.name.toLowerCase().endsWith(".ovpn") || file.size > 256 * 1024) {
      showMessage("Chọn file .ovpn không quá 256 KiB.", true);
      return;
    }
    var button = form.querySelector('button[type="submit"]');
    button.disabled = true;
    showMessage("Đang kiểm tra profile…", false);
    try {
      render(await request("/api/settings/grafana-vpn/profile", { method: "POST", body: new FormData(form) }));
      form.reset();
      showMessage("Profile đã lưu. Nhập thông tin đăng nhập nếu cần rồi bấm Kết nối VPN.", false);
    } catch (error) { showMessage(error.message, true); }
    finally { await loadStatus(); }
  });

  async function changeConnection(action) {
    var options = { method: "POST" };
    if (action === "connect" && !auth.hidden) {
      if (!username.value.trim() || !password.value) {
        showMessage("Nhập username và mật khẩu/OTP VPN trước khi kết nối.", true);
        return;
      }
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify({ username: username.value.trim(), password: password.value });
    } else if (action === "connect") {
      options.headers = { "Content-Type": "application/json" };
      options.body = JSON.stringify({});
    }
    if (action === "connect" && !window.confirm("Kết nối OpenVPN? Tunnel sẽ mở nhưng không tự thay đổi route hoặc DNS.")) return;
    if (action === "disconnect" && !window.confirm("Ngắt kết nối VPN?")) return;
    connect.disabled = true;
    disconnect.disabled = true;
    showMessage("Đang cập nhật kết nối…", false);
    try {
      render(await request("/api/settings/grafana-vpn/" + action, options));
      showMessage(action === "connect" ? "Đã gửi lệnh kết nối. Đang chờ tunnel hoạt động." : "Đã ngắt kết nối.", false);
    } catch (error) { showMessage(error.message, true); }
    finally { password.value = ""; await loadStatus(); }
  }
  connect.addEventListener("click", function () { changeConnection("connect"); });
  disconnect.addEventListener("click", function () { changeConnection("disconnect"); });
  refresh.addEventListener("click", loadStatus);
  loadStatus();
  window.setInterval(function () { if (!panel.hidden) loadStatus(); }, 15000);
}());
