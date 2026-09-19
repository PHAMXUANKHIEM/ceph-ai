"""Deterministic, evidence-first analyzers for Ceph operational snapshots.

These analyzers do not call SSH, LLMs, subprocesses, or action executors. They
only classify already collected evidence. Missing/stale data is explicit and
never becomes a false ``HEALTH_OK`` result.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping


def _dict(value: object) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _number(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _first_number(row: Mapping[str, Any], *keys: str) -> float | None:
    for key in keys:
        value = _number(row.get(key))
        if value is not None:
            return value
    return None


def _stable_id(analyzer: str, code: str, entities: Mapping[str, Any]) -> str:
    canonical = json.dumps(
        {"analyzer": analyzer, "code": code, "entities": dict(entities)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"finding-{hashlib.sha256(canonical.encode()).hexdigest()[:20]}"


@dataclass(frozen=True)
class Finding:
    analyzer: str
    finding_id: str
    code: str
    severity: str
    status: str
    summary: str
    entities: dict[str, Any] = field(default_factory=dict)
    evidence: tuple[dict[str, Any], ...] = ()
    confidence: float = 0.0
    next_checks: tuple[str, ...] = ()
    recommended_action: None = None
    evidence_gaps: tuple[str, ...] = ()
    stale: bool = False
    cluster_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        value = asdict(self)
        value["evidence"] = [dict(item) for item in self.evidence]
        value["next_checks"] = list(self.next_checks)
        value["evidence_gaps"] = list(self.evidence_gaps)
        return value


def _finding(
    analyzer: str,
    code: str,
    severity: str,
    status: str,
    summary: str,
    *,
    entities: Mapping[str, Any] | None = None,
    evidence: list[Mapping[str, Any]] | None = None,
    confidence: float = 0.9,
    next_checks: tuple[str, ...] = (),
    evidence_gaps: tuple[str, ...] = (),
    stale: bool = False,
    cluster_id: str | None = None,
) -> Finding:
    safe_entities = dict(entities or {})
    return Finding(
        analyzer=analyzer,
        finding_id=_stable_id(analyzer, code, safe_entities),
        code=code,
        severity=severity,
        status=status,
        summary=summary,
        entities=safe_entities,
        evidence=tuple(dict(item) for item in (evidence or [])),
        confidence=max(0.0, min(1.0, confidence)),
        next_checks=next_checks,
        evidence_gaps=evidence_gaps,
        stale=stale,
        cluster_id=cluster_id,
    )


def _context(snapshot: Mapping[str, Any], cluster_id: str | None) -> tuple[bool, tuple[str, ...], bool]:
    meta = _dict(snapshot.get("meta"))
    stale = bool(meta.get("stale", snapshot.get("stale", False)))
    gaps: list[str] = []
    if stale:
        gaps.append("Snapshot đã quá thời hạn freshness; cần xác minh lại trước khi hành động.")
    if meta.get("partial") or snapshot.get("partial_errors"):
        gaps.append("Evidence chỉ là partial; một hoặc nhiều section thu thập lỗi.")
    available = bool(meta.get("available", True)) and snapshot.get("data", snapshot) is not None
    if not available:
        gaps.append("Không có snapshot khả dụng cho analyzer này.")
    return available, tuple(gaps), stale


class HealthAnalyzer:
    name = "health"

    def analyze(self, snapshot: Mapping[str, Any], *, cluster_id: str | None = None) -> list[Finding]:
        available, gaps, stale = _context(snapshot, cluster_id)
        data = _dict(snapshot.get("data", snapshot))
        health = _dict(data.get("health")) or data
        status = str(health.get("status") or data.get("health_status") or "").upper()
        checks = _dict(health.get("checks") or data.get("health_checks"))
        if not available or status not in {"HEALTH_OK", "HEALTH_WARN", "HEALTH_ERR"}:
            return [_finding(
                self.name, "HEALTH_EVIDENCE_MISSING", "unknown", "INSUFFICIENT_EVIDENCE",
                "Chưa đủ evidence để kết luận sức khỏe cluster.",
                confidence=0.0, evidence_gaps=gaps or ("Thiếu health status hợp lệ.",),
                stale=stale, cluster_id=cluster_id,
            )]
        severity = {"HEALTH_OK": "info", "HEALTH_WARN": "warning", "HEALTH_ERR": "critical"}[status]
        code = {"HEALTH_OK": "HEALTH_OK", "HEALTH_WARN": "HEALTH_WARN", "HEALTH_ERR": "HEALTH_ERR"}[status]
        summary = f"Cluster đang ở trạng thái {status}."
        if checks:
            summary += f" Có {len(checks)} health check cần đối chiếu."
        return [_finding(
            self.name, code, severity, "OBSERVED", summary,
            evidence=[{"status": status, "checks": checks}],
            confidence=0.99, next_checks=("Đối chiếu health detail với snapshot mới nhất.",) if status != "HEALTH_OK" else (),
            evidence_gaps=gaps, stale=stale, cluster_id=cluster_id,
        )]


class OSDAnalyzer:
    name = "osd_health"

    def analyze(self, snapshot: Mapping[str, Any], *, cluster_id: str | None = None) -> list[Finding]:
        available, gaps, stale = _context(snapshot, cluster_id)
        data = _dict(snapshot.get("data", snapshot))
        status = _dict(data.get("status")) or data
        osdmap = _dict(status.get("osdmap"))
        rows = _list(data.get("osds")) or _list(data.get("nodes"))
        findings: list[Finding] = []
        if not available or (not osdmap and not rows):
            return [_finding(
                self.name, "OSD_EVIDENCE_MISSING", "unknown", "INSUFFICIENT_EVIDENCE",
                "Chưa đủ evidence về trạng thái OSD.", confidence=0.0,
                evidence_gaps=gaps or ("Thiếu osdmap hoặc danh sách OSD.",), stale=stale,
                cluster_id=cluster_id,
            )]
        down = _first_number(osdmap, "num_osds_down", "num_down_osds")
        total = _first_number(osdmap, "num_osds", "num_osds_total")
        if down is not None and down > 0:
            findings.append(_finding(
                self.name, "OSD_DOWN", "critical", "OBSERVED",
                f"Có {int(down)} OSD đang down.",
                entities={"count": int(down)}, evidence=[{"osdmap": osdmap}],
                confidence=0.99, next_checks=("Đối chiếu OSD tree, host và log daemon.",),
                evidence_gaps=gaps, stale=stale, cluster_id=cluster_id,
            ))
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            osd_id = row.get("osd", row.get("id"))
            state = str(row.get("status", row.get("state", ""))).lower()
            up = row.get("up")
            in_cluster = row.get("in")
            if up in (0, False) or "down" in state:
                findings.append(_finding(
                    self.name, "OSD_DOWN", "critical", "OBSERVED", f"OSD {osd_id} đang down.",
                    entities={"osd_id": osd_id}, evidence=[dict(row)], confidence=0.98,
                    next_checks=("Kiểm tra host và log OSD.",), evidence_gaps=gaps,
                    stale=stale, cluster_id=cluster_id,
                ))
            if in_cluster in (0, False) or "out" in state:
                findings.append(_finding(
                    self.name, "OSD_OUT", "warning", "OBSERVED", f"OSD {osd_id} đang out.",
                    entities={"osd_id": osd_id}, evidence=[dict(row)], confidence=0.98,
                    next_checks=("Kiểm tra nguyên nhân OSD bị đánh dấu out.",), evidence_gaps=gaps,
                    stale=stale, cluster_id=cluster_id,
                ))
            util = _first_number(row, "utilization", "utilization_percent", "percent_used", "used_percent")
            if util is not None and util >= 90:
                findings.append(_finding(
                    self.name, "OSD_NEARFULL", "critical", "OBSERVED",
                    f"OSD {osd_id} có utilization {util:.1f}%.",
                    entities={"osd_id": osd_id}, evidence=[{"utilization_percent": util}],
                    confidence=0.95, next_checks=("Kiểm tra pool/PG phân bố trên OSD này.",),
                    evidence_gaps=gaps, stale=stale, cluster_id=cluster_id,
                ))
        if not findings:
            findings.append(_finding(
                self.name, "OSD_OBSERVED", "info", "OBSERVED",
                f"OSD evidence hợp lệ{f' ({int(total)} OSD)' if total is not None else ''}; chưa thấy down/out/nearfull.",
                evidence=[{"osdmap": osdmap}], confidence=0.85, evidence_gaps=gaps,
                stale=stale, cluster_id=cluster_id,
            ))
        return findings


class PGAnalyzer:
    name = "pg_health"
    _BAD_STATES = {
        "degraded": ("warning", "PG_DEGRADED"), "undersized": ("warning", "PG_UNDERSIZED"),
        "inactive": ("critical", "PG_INACTIVE"), "stale": ("critical", "PG_STALE"),
        "stuck": ("warning", "PG_STUCK"), "peering": ("warning", "PG_PEERING"),
    }

    def analyze(self, snapshot: Mapping[str, Any], *, cluster_id: str | None = None) -> list[Finding]:
        available, gaps, stale = _context(snapshot, cluster_id)
        data = _dict(snapshot.get("data", snapshot))
        status = _dict(data.get("status")) or data
        pgmap = _dict(status.get("pgmap"))
        raw = data.get("pgs") if data.get("pgs") is not None else pgmap.get("pgs_by_state")
        rows = _list(raw)
        if not available or not pgmap and not rows:
            return [_finding(self.name, "PG_EVIDENCE_MISSING", "unknown", "INSUFFICIENT_EVIDENCE",
                "Chưa đủ evidence về trạng thái PG.", confidence=0.0,
                evidence_gaps=gaps or ("Thiếu pgmap hoặc pgs_by_state.",), stale=stale, cluster_id=cluster_id)]
        counts: dict[str, int] = {}
        for row in rows:
            if isinstance(row, Mapping):
                state = str(row.get("state_name", row.get("state", ""))).lower()
                count = _number(row.get("count", row.get("num", 1))) or 0
            else:
                state, count = str(row).lower(), 1
            for token in self._BAD_STATES:
                if token in state:
                    counts[token] = counts.get(token, 0) + int(count)
        findings: list[Finding] = []
        for token, count in sorted(counts.items()):
            severity, code = self._BAD_STATES[token]
            findings.append(_finding(
                self.name, code, severity, "OBSERVED", f"Có {count} PG ở trạng thái {token}.",
                entities={"state": token, "count": count}, evidence=[{"pgmap": pgmap, "state": token, "count": count}],
                confidence=0.98, next_checks=("Kiểm tra health detail và OSD/host ảnh hưởng.",),
                evidence_gaps=gaps, stale=stale, cluster_id=cluster_id,
            ))
        return findings or [_finding(self.name, "PG_OBSERVED", "info", "OBSERVED",
            "PG evidence hợp lệ; chưa thấy trạng thái degraded, undersized hoặc inactive.",
            evidence=[{"pgmap": pgmap}], confidence=0.85, evidence_gaps=gaps,
            stale=stale, cluster_id=cluster_id)]


class MONAnalyzer:
    name = "mon_health"

    def analyze(self, snapshot: Mapping[str, Any], *, cluster_id: str | None = None) -> list[Finding]:
        available, gaps, stale = _context(snapshot, cluster_id)
        data = _dict(snapshot.get("data", snapshot))
        status = _dict(data.get("status")) or data
        monmap = _dict(status.get("monmap"))
        mons = _list(monmap.get("mons"))
        quorum = _list(status.get("quorum_names"))
        if not available or not monmap:
            return [_finding(self.name, "MON_EVIDENCE_MISSING", "unknown", "INSUFFICIENT_EVIDENCE",
                "Chưa đủ evidence về MON quorum.", confidence=0.0,
                evidence_gaps=gaps or ("Thiếu monmap.",), stale=stale, cluster_id=cluster_id)]
        total = _first_number(monmap, "num_mons") or len(mons)
        findings: list[Finding] = []
        if total and len(quorum) < total:
            findings.append(_finding(self.name, "MON_QUORUM_DEGRADED", "critical", "OBSERVED",
                f"MON quorum thiếu: {len(quorum)}/{int(total)} MON đang trong quorum.",
                entities={"quorum": len(quorum), "total": int(total)}, evidence=[{"monmap": monmap, "quorum_names": quorum}],
                confidence=0.99, next_checks=("Kiểm tra MON không có trong quorum và clock skew.",),
                evidence_gaps=gaps, stale=stale, cluster_id=cluster_id))
        return findings or [_finding(self.name, "MON_QUORUM_OK", "info", "OBSERVED",
            f"MON quorum đầy đủ ({len(quorum)}/{int(total) if total else len(quorum)}).",
            evidence=[{"monmap": monmap, "quorum_names": quorum}], confidence=0.95,
            evidence_gaps=gaps, stale=stale, cluster_id=cluster_id)]


class PoolAnalyzer:
    name = "pool_capacity"

    def analyze(self, snapshot: Mapping[str, Any], *, cluster_id: str | None = None) -> list[Finding]:
        available, gaps, stale = _context(snapshot, cluster_id)
        data = _dict(snapshot.get("data", snapshot))
        rows = _list(data.get("pools"))
        if not available or not rows:
            return [_finding(self.name, "POOL_EVIDENCE_MISSING", "unknown", "INSUFFICIENT_EVIDENCE",
                "Chưa đủ evidence về pool capacity.", confidence=0.0,
                evidence_gaps=gaps or ("Thiếu danh sách pool.",), stale=stale, cluster_id=cluster_id)]
        findings: list[Finding] = []
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            name = row.get("pool_name", row.get("name", row.get("pool")))
            usage = _first_number(row, "used_percent", "utilization_percent", "percent_used", "usage_percent")
            if usage is not None and usage >= 90:
                findings.append(_finding(self.name, "POOL_NEARFULL", "critical", "OBSERVED",
                    f"Pool {name} đang sử dụng {usage:.1f}%.", entities={"pool": name},
                    evidence=[dict(row)], confidence=0.95, next_checks=("Kiểm tra OSD/PG phân bố và quota pool.",),
                    evidence_gaps=gaps, stale=stale, cluster_id=cluster_id))
            elif usage is not None and usage >= 80:
                findings.append(_finding(self.name, "POOL_CAPACITY_WARNING", "warning", "OBSERVED",
                    f"Pool {name} có usage {usage:.1f}%.", entities={"pool": name},
                    evidence=[dict(row)], confidence=0.95, next_checks=("Theo dõi xu hướng dung lượng pool.",),
                    evidence_gaps=gaps, stale=stale, cluster_id=cluster_id))
            replica = _first_number(row, "size", "replication_size")
            if replica is not None and replica < 2:
                findings.append(_finding(self.name, "POOL_REPLICATION_LOW", "warning", "OBSERVED",
                    f"Pool {name} có replication size {int(replica)}.", entities={"pool": name},
                    evidence=[{"size": replica}], confidence=0.9, next_checks=("Xác nhận policy dữ liệu trước khi thay đổi replication.",),
                    evidence_gaps=gaps, stale=stale, cluster_id=cluster_id))
        return findings or [_finding(self.name, "POOL_OBSERVED", "info", "OBSERVED",
            f"Đã phân tích {len(rows)} pool; chưa thấy ngưỡng capacity hoặc replication bất thường.",
            evidence=[{"pool_count": len(rows)}], confidence=0.8, evidence_gaps=gaps,
            stale=stale, cluster_id=cluster_id)]


class NodeAnalyzer:
    name = "node_metrics"

    def analyze(self, snapshot: Mapping[str, Any], *, cluster_id: str | None = None) -> list[Finding]:
        available, gaps, stale = _context(snapshot, cluster_id)
        data = _dict(snapshot.get("data", snapshot))
        rows = _list(data.get("nodes"))
        if not available or not rows:
            return [_finding(self.name, "NODE_EVIDENCE_MISSING", "unknown", "INSUFFICIENT_EVIDENCE",
                "Chưa đủ evidence metrics hoặc reachability của node.", confidence=0.0,
                evidence_gaps=gaps or ("Thiếu node metrics.",), stale=stale, cluster_id=cluster_id)]
        findings: list[Finding] = []
        thresholds = (("cpu", ("cpu_percent", "cpu"), 90, "NODE_CPU_HIGH"),
                      ("ram", ("ram_percent", "memory_percent", "ram"), 90, "NODE_RAM_HIGH"),
                      ("latency", ("latency_ms", "disk_latency_ms"), 50, "NODE_LATENCY_HIGH"))
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            host = row.get("host", row.get("hostname", row.get("ip")))
            state = str(row.get("status", row.get("state", ""))).lower()
            if state in {"offline", "unreachable", "down", "error"} or row.get("reachable") is False:
                findings.append(_finding(self.name, "NODE_UNREACHABLE", "critical", "OBSERVED",
                    f"Node {host} không reachable.", entities={"host": host}, evidence=[dict(row)],
                    confidence=0.95, next_checks=("Kiểm tra SSH/network và daemon trên node.",), evidence_gaps=gaps,
                    stale=stale, cluster_id=cluster_id))
            for metric, keys, threshold, code in thresholds:
                value = _first_number(row, *keys)
                if value is not None and value >= threshold:
                    findings.append(_finding(self.name, code, "warning", "OBSERVED",
                        f"Node {host} có {metric} {value:.1f}, vượt ngưỡng {threshold}.",
                        entities={"host": host, "metric": metric}, evidence=[{"value": value, "threshold": threshold}],
                        confidence=0.9, next_checks=("Đối chiếu thời gian quan sát và workload trên node.",),
                        evidence_gaps=gaps, stale=stale, cluster_id=cluster_id))
        return findings or [_finding(self.name, "NODE_OBSERVED", "info", "OBSERVED",
            f"Đã phân tích {len(rows)} node; chưa thấy reachability hoặc metric vượt ngưỡng.",
            evidence=[{"node_count": len(rows)}], confidence=0.8, evidence_gaps=gaps,
            stale=stale, cluster_id=cluster_id)]


class RGWAnalyzer:
    name = "rgw_health"

    def analyze(self, snapshot: Mapping[str, Any], *, cluster_id: str | None = None) -> list[Finding]:
        available, gaps, stale = _context(snapshot, cluster_id)
        data = _dict(snapshot.get("data", snapshot))
        rgw = _dict(data.get("rgw")) or data
        if not available or not rgw:
            return [_finding(self.name, "RGW_EVIDENCE_MISSING", "unknown", "INSUFFICIENT_EVIDENCE",
                "Chưa đủ evidence về RGW endpoint, daemon hoặc sync.", confidence=0.0,
                evidence_gaps=gaps or ("Thiếu RGW evidence.",), stale=stale, cluster_id=cluster_id)]
        findings: list[Finding] = []
        for key, code, severity, summary in (
            ("errors", "RGW_ERRORS", "critical", "RGW có lỗi được ghi nhận."),
            ("auth_errors", "RGW_AUTH_ERRORS", "warning", "RGW có lỗi xác thực."),
            ("quota_exceeded", "RGW_QUOTA_EXCEEDED", "warning", "RGW có quota vượt ngưỡng."),
            ("sync_error", "RGW_SYNC_ERROR", "warning", "RGW multisite sync có lỗi."),
        ):
            value = rgw.get(key)
            if value and value != 0 and value != [] and value != {}:
                findings.append(_finding(self.name, code, severity, "OBSERVED", summary,
                    entities={"signal": key}, evidence=[{key: value}], confidence=0.9,
                    next_checks=("Mở RGW evidence chi tiết và đối chiếu timestamp.",),
                    evidence_gaps=gaps, stale=stale, cluster_id=cluster_id))
        return findings or [_finding(self.name, "RGW_OBSERVED", "info", "OBSERVED",
            "RGW evidence hiện chưa ghi nhận lỗi rõ ràng.", evidence=[{"keys": sorted(rgw.keys())}],
            confidence=0.7, evidence_gaps=gaps or ("Chưa có access-log window đầy đủ; không kết luận endpoint tuyệt đối khỏe.",),
            stale=stale, cluster_id=cluster_id)]


class BackupAnalyzer:
    name = "backup_status"

    def analyze(self, snapshot: Mapping[str, Any], *, cluster_id: str | None = None) -> list[Finding]:
        available, gaps, stale = _context(snapshot, cluster_id)
        data = _dict(snapshot.get("data", snapshot))
        backup = _dict(data.get("backup")) or data
        if not available or not backup:
            return [_finding(self.name, "BACKUP_EVIDENCE_MISSING", "unknown", "INSUFFICIENT_EVIDENCE",
                "Chưa đủ evidence về backup, RPO/RTO hoặc metadata.", confidence=0.0,
                evidence_gaps=gaps or ("Thiếu backup snapshot.",), stale=stale, cluster_id=cluster_id)]
        findings: list[Finding] = []
        failed = _first_number(backup, "failed_jobs", "failures", "failed")
        if failed and failed > 0:
            findings.append(_finding(self.name, "BACKUP_FAILED", "critical", "OBSERVED",
                f"Có {int(failed)} backup job thất bại.", entities={"failed_jobs": int(failed)},
                evidence=[{"failed_jobs": failed}], confidence=0.98, next_checks=("Kiểm tra job detail và nguyên nhân backend.",),
                evidence_gaps=gaps, stale=stale, cluster_id=cluster_id))
        rpo_breaches = _first_number(backup, "rpo_breaches", "rpo_violations", "violations")
        if rpo_breaches and rpo_breaches > 0:
            findings.append(_finding(self.name, "BACKUP_RPO_BREACH", "critical", "OBSERVED",
                f"Có {int(rpo_breaches)} vi phạm RPO.", entities={"rpo_breaches": int(rpo_breaches)},
                evidence=[{"rpo_breaches": rpo_breaches}], confidence=0.98, next_checks=("Kiểm tra lần backup thành công gần nhất.",),
                evidence_gaps=gaps, stale=stale, cluster_id=cluster_id))
        return findings or [_finding(self.name, "BACKUP_OBSERVED", "info", "OBSERVED",
            "Backup evidence hiện chưa ghi nhận job thất bại hoặc vi phạm RPO.",
            evidence=[{"keys": sorted(backup.keys())}], confidence=0.75,
            evidence_gaps=gaps or ("Chưa có restore-drill evidence; không kết luận khả năng khôi phục.",),
            stale=stale, cluster_id=cluster_id)]


class CrushAnalyzer:
    name = "crush_analysis"

    def analyze(self, snapshot: Mapping[str, Any], *, cluster_id: str | None = None) -> list[Finding]:
        available, gaps, stale = _context(snapshot, cluster_id)
        data = _dict(snapshot.get("data", snapshot))
        crush = _dict(data.get("crush")) or data
        nodes = _list(crush.get("nodes")) or _list(crush.get("tree"))
        if not available or not nodes:
            return [_finding(self.name, "CRUSH_EVIDENCE_MISSING", "unknown", "INSUFFICIENT_EVIDENCE",
                "Chưa đủ evidence về cây CRUSH.", confidence=0.0,
                evidence_gaps=gaps or ("Thiếu CRUSH tree.",), stale=stale, cluster_id=cluster_id)]
        missing_hosts = [row for row in nodes if isinstance(row, Mapping) and row.get("type") == "osd" and not row.get("host")]
        if missing_hosts:
            return [_finding(self.name, "CRUSH_OSD_HOST_MISSING", "warning", "OBSERVED",
                f"Có {len(missing_hosts)} OSD chưa map được host trong CRUSH evidence.",
                entities={"count": len(missing_hosts)}, evidence=[{"osds": missing_hosts[:20]}],
                confidence=0.9, next_checks=("Kiểm tra CRUSH tree và failure domain mapping.",),
                evidence_gaps=gaps, stale=stale, cluster_id=cluster_id)]
        return [_finding(self.name, "CRUSH_OBSERVED", "info", "OBSERVED",
            f"CRUSH evidence hợp lệ với {len(nodes)} node.", evidence=[{"node_count": len(nodes)}],
            confidence=0.75, evidence_gaps=gaps or ("Chưa có phân tích skew/failure-domain đầy đủ.",),
            stale=stale, cluster_id=cluster_id)]


ANALYZERS = {
    analyzer.name: analyzer
    for analyzer in (
        HealthAnalyzer(), OSDAnalyzer(), PGAnalyzer(), MONAnalyzer(), PoolAnalyzer(),
        NodeAnalyzer(), RGWAnalyzer(), BackupAnalyzer(), CrushAnalyzer(),
    )
}


def analyze_evidence(
    analyzer: str,
    snapshot: Mapping[str, Any],
    *,
    cluster_id: str | None = None,
) -> list[Finding]:
    """Run a named deterministic analyzer; unknown names fail closed."""

    try:
        selected = ANALYZERS[analyzer]
    except KeyError as exc:
        raise ValueError(f"unknown deterministic analyzer: {analyzer!r}") from exc
    return selected.analyze(snapshot, cluster_id=cluster_id)

