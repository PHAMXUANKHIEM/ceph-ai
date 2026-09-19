from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_volumes_page_exposes_snapshot_clone_dependency_panel():
    template = (ROOT / "dashboard/templates/volumes.html").read_text()
    script = (ROOT / "dashboard/static/volume_inventory.js").read_text()
    stylesheet = (ROOT / "dashboard/static/volume_inventory_redesign.css").read_text()

    assert 'id="volume-dependency-insights"' in template
    assert "/snapshot-clone-insights" in script
    assert "volume-dependency-item" in stylesheet
