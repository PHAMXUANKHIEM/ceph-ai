"""Log Intelligence — form cấu hình trên trang Settings.

Trước đây chỉ sửa được bằng tay trong `.env` rồi restart, trong khi mọi
cấu hình khác (cụm Ceph, router AI, database, patch pipeline) đều sửa được
trên giao diện. File này khoá hành vi của form mới.
"""

from config.settings import settings


def _login(client):
    client.post("/login", data={"username": "admin", "password": "admin"})


def _form(**overrides):
    data = {
        "log_intel_source": "ssh",
        "log_intel_scan_interval_seconds": "900",
        "log_intel_window_minutes": "60",
        "log_intel_max_lines_per_daemon": "5000",
        "log_intel_loki_url": "",
        "log_intel_loki_tenant": "",
    }
    data.update(overrides)
    return data


def _no_restart(monkeypatch):
    """Form thật sẽ restart Watcher — chặn lại trong test."""
    import dashboard.routes.settings as settings_routes

    monkeypatch.setattr(settings_routes, "restart_watcher", lambda: {"ok": True})


def _no_env_write(monkeypatch):
    import dashboard.routes.settings as settings_routes

    monkeypatch.setattr(settings_routes, "_update_env_file_batch", lambda values: None)


# --- Hiển thị -------------------------------------------------------------


def test_settings_page_has_log_intelligence_section(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.get("/settings")
    assert response.status_code == 200
    assert "Log Intelligence" in response.text
    assert 'name="log_intel_loki_url"' in response.text
    assert "Kiểm tra kết nối Loki" in response.text


def test_page_states_the_recommended_enable_order(dashboard_client):
    """Bật AI ngay từ đầu là cách nhanh nhất để đốt token vào nhiễu — thứ tự
    khuyến nghị phải nằm ngay trên form, không chỉ trong runbook."""
    _login(dashboard_client)
    response = dashboard_client.get("/settings")
    assert "không tốn token" in response.text
    assert "3–7 ngày" in response.text


# --- Lưu cấu hình ---------------------------------------------------------


def test_save_ssh_source(dashboard_client, monkeypatch):
    _no_restart(monkeypatch)
    _no_env_write(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/log-intel", data=_form(log_intel_enabled="1")
    )

    assert response.status_code == 200
    assert "Đã lưu cấu hình" in response.text
    assert settings.log_intel_enabled is True
    assert settings.log_intel_source == "ssh"


def test_unchecked_switch_saves_as_false(dashboard_client, monkeypatch):
    """Checkbox không tick thì trình duyệt không gửi field — phải hiểu là
    TẮT, không phải giữ nguyên giá trị cũ."""
    _no_restart(monkeypatch)
    _no_env_write(monkeypatch)
    monkeypatch.setattr(settings, "log_intel_ai_enabled", True)
    _login(dashboard_client)

    dashboard_client.post("/settings/log-intel", data=_form(log_intel_enabled="1"))

    assert settings.log_intel_ai_enabled is False


def test_loki_source_without_url_is_rejected(dashboard_client, monkeypatch):
    """Chặn NGAY tại form thay vì để lưu rồi mới hỏng lúc quét: một cấu hình
    thiếu mà im lặng trông y hệt một cụm không phát sinh log nào."""
    _no_restart(monkeypatch)
    _no_env_write(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/log-intel", data=_form(log_intel_source="loki", log_intel_loki_url="")
    )

    assert "bắt buộc phải điền Loki URL" in response.text


def test_loki_url_must_be_http(dashboard_client, monkeypatch):
    _no_restart(monkeypatch)
    _no_env_write(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/log-intel",
        data=_form(log_intel_source="loki", log_intel_loki_url="loki.local:3100"),
    )

    assert "http:// hoặc https://" in response.text


def test_window_must_exceed_scan_interval(dashboard_client, monkeypatch):
    """Cửa sổ <= chu kỳ quét sẽ để lại lỗ hổng dữ liệu vĩnh viễn mỗi khi có
    một tick chạy chậm — chặn ở form vì sau đó không có cách nào lấy lại."""
    _no_restart(monkeypatch)
    _no_env_write(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/log-intel",
        data=_form(log_intel_scan_interval_seconds="3600", log_intel_window_minutes="60"),
    )

    assert "phải LỚN HƠN chu kỳ quét" in response.text


def test_bad_source_value_is_rejected(dashboard_client, monkeypatch):
    _no_restart(monkeypatch)
    _no_env_write(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/log-intel", data=_form(log_intel_source="elasticsearch")
    )

    assert "chỉ nhận" in response.text


def test_enabling_ai_warns_about_token_cost(dashboard_client, monkeypatch):
    _no_restart(monkeypatch)
    _no_env_write(monkeypatch)
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/log-intel",
        data=_form(log_intel_enabled="1", log_intel_ai_enabled="1"),
    )

    assert "chi phí token" in response.text


def test_saving_restarts_watcher_not_worker(dashboard_client, monkeypatch):
    """Watcher là tiến trình chạy vòng quét này — restart Worker sẽ không áp
    dụng gì mà còn làm gián đoạn việc thực thi action đang chờ."""
    import dashboard.routes.settings as settings_routes

    called = []
    monkeypatch.setattr(settings_routes, "restart_watcher", lambda: called.append("watcher"))
    monkeypatch.setattr(settings_routes, "restart_worker", lambda: called.append("worker"))
    _no_env_write(monkeypatch)
    _login(dashboard_client)

    dashboard_client.post("/settings/log-intel", data=_form(log_intel_enabled="1"))

    assert called == ["watcher"]


# --- Nút test kết nối Loki ------------------------------------------------


def test_test_loki_without_url_returns_message(dashboard_client):
    _login(dashboard_client)
    response = dashboard_client.post(
        "/settings/log-intel/test-loki", data={"log_intel_loki_url": ""}
    )
    assert response.json() == {"ok": False, "message": "Chưa điền Loki URL."}


def test_test_loki_reports_unreachable_without_saving(dashboard_client):
    """Kiểm tra kết nối KHÔNG được lưu gì — chỉ báo kết quả."""
    before = settings.log_intel_loki_url
    _login(dashboard_client)

    response = dashboard_client.post(
        "/settings/log-intel/test-loki",
        data={"log_intel_loki_url": "http://127.0.0.1:1", "log_intel_loki_tenant": ""},
    )

    body = response.json()
    assert body["ok"] is False
    assert "Không kết nối được" in body["message"]
    assert settings.log_intel_loki_url == before
