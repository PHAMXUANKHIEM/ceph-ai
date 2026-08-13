import pytest

from vitastor.diagnosis import parse_diagnosis


def test_parse_diagnosis_accepts_required_json_contract():
    result = parse_diagnosis('''```json
{"root_cause":"OSD 3 down","impact":"Reduced redundancy","confidence":"high","evidence":["OSD 2/3 up"],"recommended_steps":["Inspect OSD 3"],"commands_preview":["vitastor-cli osd-tree -l"],"safety_notes":["Không xoá OSD khi chưa dry-run"]}
```''')
    assert result["confidence"] == "high"
    assert result["commands_preview"] == ["vitastor-cli osd-tree -l"]


def test_parse_diagnosis_rejects_unstructured_or_incomplete_output():
    with pytest.raises(ValueError): parse_diagnosis("Có thể OSD đang down")
    with pytest.raises(ValueError): parse_diagnosis('{"root_cause":"unknown"}')
