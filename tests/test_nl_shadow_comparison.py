import json
from pathlib import Path


REPORT = json.loads(
    (Path(__file__).parents[1] / "docs/ai/nl-shadow-comparison.json").read_text(
        encoding="utf-8"
    )
)


def test_shadow_comparison_covers_all_cases_and_is_redacted():
    assert REPORT["schema_version"] == "nl-shadow-compare-v1"
    assert REPORT["case_count"] == 100
    assert REPORT["changed_count"] + REPORT["unchanged_count"] == 100
    assert REPORT["all_text_redacted"] is True
    assert len(REPORT["changes"]) == REPORT["changed_count"]
    for change in REPORT["changes"]:
        text = change["text"].casefold()
        assert "password=" not in text
        assert "secret" not in text
        assert "token=" not in text
