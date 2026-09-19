"""Deterministic intent router for common Vietnamese Ceph questions."""

from __future__ import annotations

from .normalizer import detect_language, extract_entities, extract_time_range, fold_text
from .schema import NaturalLanguageIntent, TimeRange


_MUTATION_TERMS = (
    "xoa", "tao", "tao moi", "resize", "restart", "repair", "delete",
    "create", "remove", "purge", "out osd", "in osd", "sua", "doi",
    "thay doi", "bat", "tat", "khoi phuc", "restore",
)

_INTENT_RULES: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("log_search", ("log", "logs", "nhat ky", "audit"), "log"),
    ("rgw_diagnosis", ("rgw", "s3", "bucket", "object storage"), "rgw"),
    ("crush_analysis", ("crush", "failure domain"), "crush"),
    ("backup_status", ("backup", "rpo", "rto", "sao luu"), "backup"),
    ("volume_insight", ("volume", "rbd", "cinder", "block storage"), "volume"),
    ("pg_health", (" pg ", "placement group", "degraded", "undersized", "inactive", "stuck"), "pg"),
    ("osd_health", ("osd", "mon", "mgr", "disk"), "osd"),
    ("node_metrics", ("cpu", "ram", "iops", "latency", "node", "may chu"), "node"),
    ("pool_capacity", ("pool", "dung luong", "capacity", "nearfull", "full"), "pool"),
    ("cluster_health", ("health", "suc khoe", "trang thai", "tong quan", "co van de"), "cluster"),
)


def _contains(folded: str, keyword: str) -> bool:
    if keyword.startswith(" ") or keyword.endswith(" "):
        return keyword in f" {folded} "
    return keyword in folded


def route_natural_language(
    text: str,
    *,
    cluster_id: str | None = None,
) -> NaturalLanguageIntent:
    """Classify a request without calling an LLM or executing a command."""

    original = str(text or "").strip()
    folded = fold_text(original)
    language = detect_language(original)
    resource_type, resource_ids, filters = extract_entities(original)
    duration_seconds, duration_label = extract_time_range(original)
    time_range = TimeRange(duration_seconds, duration_label)

    if not folded:
        return NaturalLanguageIntent(
            intent="unknown_or_ambiguous", language="unknown", original_text=original,
            normalized_text=folded, cluster_id=cluster_id, mode="read_only",
            confidence=0.0, needs_clarification=True,
            clarification_question="Bạn muốn kiểm tra thành phần nào của cụm Ceph?",
        )

    if any(_contains(folded, term) for term in _MUTATION_TERMS):
        return NaturalLanguageIntent(
            intent="unknown_or_ambiguous", language=language, original_text=original,
            normalized_text=folded, cluster_id=cluster_id, resource_type=resource_type,
            resource_ids=resource_ids, filters=filters, time_range=time_range,
            mode="read_only", confidence=0.2, needs_clarification=True,
            clarification_question=(
                "Bước này chỉ phân tích read-only. Bạn muốn xem trạng thái hoặc "
                "tạo preview hành động để phê duyệt?"
            ),
        )

    matches: list[tuple[str, str | None]] = []
    for intent, keywords, default_resource in _INTENT_RULES:
        if any(_contains(folded, keyword) for keyword in keywords):
            matches.append((intent, default_resource))

    if not matches:
        return NaturalLanguageIntent(
            intent="unknown_or_ambiguous", language=language, original_text=original,
            normalized_text=folded, cluster_id=cluster_id, resource_type=resource_type,
            resource_ids=resource_ids, filters=filters, time_range=time_range,
            mode="read_only", confidence=0.35, needs_clarification=True,
            clarification_question=(
                "Bạn muốn xem health, OSD/PG, dung lượng, log hay backup của cụm?"
            ),
        )

    intent, default_resource = matches[0]
    if len(matches) > 1:
        confidence = 0.62
    else:
        confidence = 0.9
    return NaturalLanguageIntent(
        intent=intent, language=language, original_text=original,
        normalized_text=folded, cluster_id=cluster_id,
        resource_type=resource_type or default_resource, resource_ids=resource_ids,
        filters=filters, time_range=time_range, mode="read_only",
        confidence=confidence,
    )

