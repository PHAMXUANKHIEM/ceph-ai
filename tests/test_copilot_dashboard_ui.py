from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dashboard_exposes_operations_copilot_entry_and_suggestions():
    template = (ROOT / "dashboard/templates/index.html").read_text()
    assert 'href="/?copilot=1"' in template
    assert "AI Operations Copilot" in template
    assert template.count("data-copilot-prompt=") == 3


def test_chat_widget_renders_verified_sources_as_evidence_panel():
    script = (ROOT / "dashboard/static/chat_widget.js").read_text()
    assert 'marker = "\\n\\nNguồn đã kiểm chứng:\\n"' in script
    assert 'sources.className = "copilot-evidence-sources"' in script
    assert 'get("copilot") === "1"' in script
