from shared.natural_language.query_executor import (
    _bounded_result,
    _serialized_size,
)


def test_large_collection_summary_reduces_prompt_payload_by_at_least_half():
    raw = {
        "osds": [
            {"id": index, "status": "up", "host": f"ceph-{index % 12}", "weight": 1.0}
            for index in range(500)
        ]
    }
    bounded, summarized = _bounded_result(raw, 200_000)
    assert summarized is True
    assert _serialized_size(bounded) <= _serialized_size(raw) * 0.5
