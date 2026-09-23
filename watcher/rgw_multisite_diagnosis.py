"""Deterministic, read-only diagnosis for RGW multisite evidence."""

from __future__ import annotations

import re
import math
from collections.abc import Iterable


LAG_WARNING_SECONDS = 300
LAG_CRITICAL_SECONDS = 3600
MAX_EVIDENCE_ITEMS = 20
MAX_WALK_ITEMS = 1000
MAX_WALK_DEPTH = 12


def _finding(code: str, severity: str, summary: str, evidence: list[str], next_check: str) -> dict:
    return {
        "code": code,
        "severity": severity,
        "summary": summary,
        "evidence": evidence[:MAX_EVIDENCE_ITEMS],
        "next_check": next_check,
        "read_only": True,
        "action_id": None,
    }


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _details(section: dict | None) -> dict | list:
    section = _mapping(section)
    value = section.get("details")
    return value if isinstance(value, (dict, list)) else {}


def _walk(value, path: tuple[str, ...] = ()) -> Iterable[tuple[tuple[str, ...], object]]:
    stack = [(path, value, 0)]
    visited = 0
    while stack and visited < MAX_WALK_ITEMS:
        current_path, current, depth = stack.pop()
        visited += 1
        yield current_path, current
        if depth >= MAX_WALK_DEPTH:
            continue
        if isinstance(current, dict):
            children = [(current_path + (str(key).casefold(),), child, depth + 1)
                        for key, child in list(current.items())[:100]]
        elif isinstance(current, list):
            children = [(current_path + (str(index),), child, depth + 1)
                        for index, child in enumerate(current[:100])]
        else:
            children = []
        stack.extend(reversed(children))


def _number(value) -> float | int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(-?\d+(?:\.\d+)?)\s*", value)
        if match:
            number = float(match.group(1))
            if math.isfinite(number):
                return int(number) if number.is_integer() else number
    return None


def _first_number(value, keys: set[str]) -> tuple[str, float | int] | None:
    for path, child in _walk(value):
        if not path or path[-1] not in keys:
            continue
        number = _number(child)
        if number is not None and number >= 0:
            return path[-1], number
    return None


def _sync_lag(sync: dict | None) -> dict:
    details = _details(sync)
    candidates = []
    for keys, unit in (
        ({"lag_seconds", "behind_seconds", "latency_seconds"}, 1),
        ({"lag_minutes", "behind_minutes"}, 60),
        ({"lag_hours", "behind_hours"}, 3600),
    ):
        found = _first_number(details, keys)
        if found:
            key, value = found
            candidates.append({"source": key, "seconds": value * unit})
    text_parts = []
    for path, value in _walk(details):
        if isinstance(value, str) and any(token in ".".join(path) for token in ("info", "status", "lag", "behind")):
            text_parts.append(value)
    text = " ".join(text_parts)
    match = re.search(r"(?:behind|lag(?:ging)?)[^0-9]{0,24}(\d+(?:\.\d+)?)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?)", text, re.I)
    if match:
        value = float(match.group(1))
        unit = match.group(2).casefold()
        multiplier = 3600 if unit.startswith("hour") or unit.startswith("hr") else 60 if unit.startswith("min") else 1
        if math.isfinite(value * multiplier):
            candidates.append({"source": "sync_info", "seconds": value * multiplier})
    if not candidates:
        return {"status": "not_observed", "seconds": None, "observations": []}
    selected = max(candidates, key=lambda item: item["seconds"])
    return {"status": "observed", "seconds": selected["seconds"], "observations": candidates[:MAX_EVIDENCE_ITEMS]}


def _sync_states(sync: dict | None) -> list[str]:
    details = _details(sync)
    states = []
    state_keys = {"sync_status", "status", "state", "metadata_status", "data_status"}
    for path, value in _walk(details):
        if path and path[-1] in state_keys and isinstance(value, str) and value.strip():
            states.append(value.strip().casefold())
    return sorted(set(states))[:MAX_EVIDENCE_ITEMS]


def _shard_errors(sync: dict | None, sync_errors: dict | None) -> list[str]:
    findings = []
    for source, value in (("sync_status", _details(sync)), ("sync_error_list", _details(sync_errors))):
        for path, child in _walk(value):
            key = path[-1] if path else ""
            if key not in {"error", "errors", "error_count", "failed_shards", "failures", "shard_errors"}:
                continue
            number = _number(child)
            if number is not None:
                if number > 0:
                    findings.append(f"{source}:{key}={number}")
            elif isinstance(child, str) and child.strip():
                findings.append(f"{source}:{key}={child.strip()[:220]}")
            elif isinstance(child, list) and child:
                findings.append(f"{source}:{key}={len(child)} item(s)")
    return sorted(set(findings))[:MAX_EVIDENCE_ITEMS]


def _master_observations(realm: dict | None, zonegroup: dict | None, zone: dict | None, period: dict | None) -> list[dict]:
    observations = []
    for source, section in (("realm", realm), ("zonegroup", zonegroup), ("zone", zone), ("period", period)):
        details = _details(section)
        for path, value in _walk(details):
            key = path[-1] if path else ""
            if key in {"master_zone", "master_zonegroup", "master", "is_master"} and value not in (None, ""):
                observations.append({"source": source, "field": key, "value": value})
    return observations[:MAX_EVIDENCE_ITEMS]


def _period_observations(realm: dict | None, zonegroup: dict | None, zone: dict | None, period: dict | None) -> list[dict]:
    observations = []
    keys = {"epoch", "period_epoch", "current_epoch", "current_period", "period_id"}
    for source, section in (("realm", realm), ("zonegroup", zonegroup), ("zone", zone), ("period", period)):
        for path, value in _walk(_details(section)):
            key = path[-1] if path else ""
            if key in keys and value not in (None, "", []):
                observations.append({"source": source, "field": key, "value": value})
    return observations[:MAX_EVIDENCE_ITEMS]


def _conflict_evidence(sync: dict | None, sync_errors: dict | None) -> list[str]:
    tokens = ("conflict", "conflicted", "conflicts")
    evidence = []
    for source, details in (("sync_status", _details(sync)), ("sync_error_list", _details(sync_errors))):
        for path, value in _walk(details):
            if not path or not any(token in path[-1] for token in tokens):
                continue
            number = _number(value)
            if number is not None and number <= 0:
                continue
            if value not in (None, "", [], {}):
                evidence.append(f"{source}:{path[-1]}={str(value)[:220]}")
    return sorted(set(evidence))[:MAX_EVIDENCE_ITEMS]


def build_multisite_diagnosis(rgw_evidence: dict | None) -> dict:
    """Analyze RGW multisite state without inferring from missing evidence."""
    evidence = _mapping(rgw_evidence)
    stale = bool(_mapping(evidence.get("cache")).get("stale") or evidence.get("stale"))
    topology = _mapping(evidence.get("topology"))
    realm = _mapping(topology.get("realm"))
    zonegroup = _mapping(topology.get("zonegroup"))
    zone = _mapping(topology.get("zone"))
    period = _mapping(evidence.get("period"))
    sync = _mapping(evidence.get("sync"))
    sync_errors = _mapping(evidence.get("sync_errors"))
    gaps = list(evidence.get("evidence_gaps") or [])
    if stale:
        gaps.append("RGW multisite snapshot đã cũ; findings chỉ phản ánh lần thu thập trước, không phải trạng thái hiện tại.")
    findings = []
    lag = _sync_lag(sync)
    states = _sync_states(sync)
    shard_errors = _shard_errors(sync, sync_errors)
    masters = _master_observations(realm, zonegroup, zone, period)
    periods = _period_observations(realm, zonegroup, zone, period)
    conflicts = _conflict_evidence(sync, sync_errors)

    observed_sections = [realm, zonegroup, zone, period, sync]
    if not any(section.get("status") == "observed" for section in observed_sections):
        gaps.append("Chưa có topology hoặc sync evidence đủ để chẩn đoán RGW multisite.")
    if sync.get("status") != "observed":
        gaps.append("Chưa đọc được sync status; không suy luận replication lag hoặc shard state.")
    if period.get("status") != "observed":
        gaps.append("Chưa đọc được period get; không xác nhận được epoch/master state đầy đủ.")
    if not masters:
        gaps.append("Không thấy trường master zone/master state trong evidence hiện tại.")
    if not periods:
        gaps.append("Không thấy epoch/period identifier trong topology evidence hiện tại.")

    if lag["status"] == "observed":
        seconds = float(lag["seconds"])
        if seconds >= LAG_CRITICAL_SECONDS:
            findings.append(_finding(
                "RGW_MULTISITE_REPLICATION_LAG", "critical",
                "RGW multisite đang có replication lag lớn hơn ngưỡng critical đã cấu hình.",
                [f"lag_seconds={seconds:g}", *[f"{item['source']}={item['seconds']:g}s" for item in lag["observations"]]],
                "Đối chiếu sync status theo từng source/shard, network và backend pool; chưa tự kích hoạt resync.",
            ))
        elif seconds >= LAG_WARNING_SECONDS:
            findings.append(_finding(
                "RGW_MULTISITE_REPLICATION_LAG", "warning",
                "RGW multisite đang có replication lag vượt ngưỡng cảnh báo đã cấu hình.",
                [f"lag_seconds={seconds:g}"],
                "Theo dõi lại sync status và kiểm tra source/target zone trước khi xử lý.",
            ))
    elif any(token in state for state in states for token in ("behind", "lag", "syncing", "error")):
        findings.append(_finding(
            "RGW_MULTISITE_SYNC_NOT_CAUGHT_UP", "warning",
            "Sync status cho thấy multisite chưa caught up nhưng chưa có số giây lag định lượng.",
            states,
            "Lấy lại sync status với thông tin lag theo source/shard; không suy luận thời điểm hoàn tất.",
        ))

    if shard_errors:
        findings.append(_finding(
            "RGW_MULTISITE_SHARD_ERROR", "critical",
            "Evidence RGW ghi nhận lỗi hoặc shard thất bại trong luồng multisite.",
            shard_errors,
            "Xem chi tiết shard/error list và log RGW; chỉ lập kế hoạch retry sau khi xác định nguyên nhân.",
        ))

    if conflicts:
        findings.append(_finding(
            "RGW_MULTISITE_CONFLICT", "critical",
            "Evidence RGW ghi nhận conflict trong quá trình đồng bộ multisite.",
            conflicts,
            "Đối chiếu source zone, conflict log và period trước khi xử lý thủ công; không tự resolve conflict.",
        ))

    comparable_periods = []
    for item in periods:
        value = item["value"]
        normalized = str(value).strip()
        field = item["field"]
        family = "period" if field in {"current_period", "period_id"} else "epoch" if field in {"epoch", "period_epoch", "current_epoch"} else None
        if normalized and family:
            comparable_periods.append((family, item["source"], field, normalized))
    mismatch_groups = {
        family: {(source, field, value) for _family, source, field, value in comparable_periods if _family == family}
        for family in {item[0] for item in comparable_periods}
    }
    mismatched = {
        family: values
        for family, values in mismatch_groups.items()
        if len({value for _source, _field, value in values}) > 1
    }
    if mismatched:
        findings.append(_finding(
            "RGW_MULTISITE_PERIOD_EPOCH_MISMATCH", "critical",
            "Các topology section đang báo period/epoch identifier khác nhau.",
            [f"{source}.{field}={value}" for values in mismatched.values() for source, field, value in sorted(values)],
            "Kiểm tra period map và trạng thái propagation giữa master zone với secondary zone; không tự commit period.",
        ))

    master_groups = {}
    for item in masters:
        field = item["field"]
        family = "zone" if field == "master_zone" else "zonegroup" if field == "master_zonegroup" else "boolean" if field in {"master", "is_master"} else field
        master_groups.setdefault(family, set()).add(str(item["value"]).strip())
    mismatched_masters = {family: values for family, values in master_groups.items() if len(values) > 1}
    if mismatched_masters:
        findings.append(_finding(
            "RGW_MULTISITE_MASTER_STATE_CONFLICT", "critical",
            "Evidence topology không nhất quán về master zone/master state.",
            [f"{item['source']}.{item['field']}={item['value']}" for item in masters if str(item["value"]).strip() in mismatched_masters.get("zone", set()) | mismatched_masters.get("zonegroup", set()) | mismatched_masters.get("boolean", set())],
            "Đối chiếu realm, zonegroup và period trên cluster; không tự thay đổi master zone.",
        ))

    if stale:
        status = "stale_evidence"
        summary = "Dữ liệu multisite đã cũ; cần thu thập lại trước khi kết luận hoặc xử lý sự cố."
    elif not findings:
        status = "insufficient_evidence" if gaps else "no_anomaly"
        summary = "Chưa thấy bất thường multisite đủ mạnh trong evidence hiện có." if status == "no_anomaly" else "Chưa đủ evidence để kết luận trạng thái RGW multisite."
    else:
        status = "diagnosed"
        summary = "; ".join(finding["summary"] for finding in findings)
    return {
        "status": status,
        "stale": stale,
        "evidence_current": not stale,
        "summary": summary,
        "findings": findings,
        "observed": {
            "sync_states": states,
            "lag": lag,
            "master_observations": masters,
            "period_observations": periods,
            "shard_error_count": len(shard_errors),
            "conflict_count": len(conflicts),
        },
        "evidence": ["rgw_topology", "rgw_sync_status", "rgw_period"],
        "evidence_gaps": sorted(set(gaps)),
        "recommendation_mode": "ADVISORY",
        "read_only": True,
        "action_id": None,
    }
