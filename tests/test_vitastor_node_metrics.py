from vitastor.node_metrics import _smart_summary


def test_nvme_smart_summary_extracts_wear_temperature_and_errors():
    result = _smart_summary({
        "smart_status": {"passed": True},
        "nvme_smart_health_information_log": {
            "temperature": 54, "percentage_used": 17,
            "media_errors": 2, "num_err_log_entries": 3,
        },
    })
    assert result == {"temperature_c": 54, "wear_percent": 17, "media_errors": 5, "smart_passed": True}


def test_ata_smart_summary_extracts_common_attributes():
    result = _smart_summary({
        "smart_status": {"passed": False},
        "ata_smart_attributes": {"table": [
            {"name": "Temperature_Celsius", "raw": {"value": 61}},
            {"name": "Media_Wearout_Indicator", "raw": {"value": 82}},
            {"name": "Reported_Uncorrect", "raw": {"value": 4}},
        ]},
    })
    assert result["temperature_c"] == 61
    assert result["wear_percent"] == 82
    assert result["media_errors"] == 4
    assert result["smart_passed"] is False
