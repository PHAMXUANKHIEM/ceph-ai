from pathlib import Path


def test_performance_rca_template_exposes_read_only_guidance():
    template = (
        Path(__file__).resolve().parents[1]
        / "dashboard"
        / "templates"
        / "performance_rca.html"
    ).read_text(encoding="utf-8")

    assert "Ranked read-only options" in template
    assert "a.investigation_steps" in template
    assert "option.next_checks" in template
    assert "không tạo Action" in template
    assert "network peer" in template
