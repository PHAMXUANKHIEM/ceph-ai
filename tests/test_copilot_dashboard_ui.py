from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_exposes_operations_copilot_entry_and_suggestions():
    template = (ROOT / "dashboard/templates/index.html").read_text()
    copilot = (ROOT / "dashboard/templates/copilot.html").read_text()
    assert 'href="/ai-copilot"' in template
    assert "AI Operations Copilot" in copilot
    assert copilot.count("data-prompt=") == 3
    assert "INCIDENT ĐANG MỞ" in copilot
    assert "CAPACITY SAMPLES" in copilot


def test_chat_widget_renders_verified_sources_as_evidence_panel():
    script = (ROOT / "dashboard/static/chat_widget.js").read_text()
    assert 'marker = "\\n\\nNguồn đã kiểm chứng:\\n"' in script
    assert 'sources.className = "copilot-evidence-sources"' in script


def test_dedicated_copilot_page_has_its_own_query_client():
    script = (ROOT / "dashboard/static/copilot.js").read_text()
    assert 'document.getElementById("copilot-query-form")' in script
    assert 'fetch("/api/chat/messages"' in script
    assert 'sources.className = "copilot-evidence-sources"' in script


def test_dedicated_copilot_route_renders_operational_evidence(dashboard_client):
    dashboard_client.post("/login", data={"username": "admin", "password": "admin"})
    response = dashboard_client.get("/ai-copilot")
    assert response.status_code == 200
    assert "AI Operations Copilot" in response.text
    assert "TIMELINE EVENTS ĐÃ LƯU" in response.text
    assert "Copilot chỉ trả lời hoặc lập kế hoạch" in response.text
