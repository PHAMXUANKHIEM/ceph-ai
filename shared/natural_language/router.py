"""Deterministic intent router for common Vietnamese Ceph questions."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import re

from .normalizer import detect_language, extract_entities, extract_time_range, fold_text
from .schema import NaturalLanguageIntent, TimeRange


_MUTATION_TERMS = (
    "xoa", "tao", "tao moi", "resize", "restart", "repair", "delete",
    "create", "remove", "purge", "out osd", "in osd", "sua", "doi",
    "thay doi", "bat", "tat", "khoi phuc", "restore",
)

_INTENT_RULES: tuple[tuple[str, tuple[str, ...], str | None], ...] = (
    ("explain_incident", ("incident", "su co", "postmortem", "root cause", "nguyen nhan"), "cluster"),
    ("recommend_action", ("de xuat", "khuyen nghi", "recommend", "nen lam gi", "cach xu ly", "goi y"), "cluster"),
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
    normalized_keyword = keyword.strip()
    if not normalized_keyword:
        return False
    escaped = normalized_keyword.replace(" ", r"\s+")
    return bool(re.search(rf"(?<![A-Za-z0-9]){escaped}(?![A-Za-z0-9])", folded))


def _route_natural_language_uncached(
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
            decision_reason="mutation_language_blocked",
        )

    if len(resource_ids) > 1:
        return NaturalLanguageIntent(
            intent="unknown_or_ambiguous", language=language, original_text=original,
            normalized_text=folded, cluster_id=cluster_id, resource_type=resource_type,
            resource_ids=resource_ids, filters=filters, time_range=time_range,
            mode="read_only", confidence=0.55, needs_clarification=True,
            clarification_question=(
                f"Tôi nhận thấy nhiều {resource_type or 'resource'} ({', '.join(resource_ids)}). "
                "Bạn muốn ưu tiên một resource cụ thể nào?"
            ),
            decision_reason="multiple_same_type_entities",
        )

    matches: list[tuple[str, str | None]] = []
    for intent, keywords, default_resource in _INTENT_RULES:
        if any(_contains(folded, keyword) for keyword in keywords):
            matches.append((intent, default_resource))

    # Health/status words are broad fallbacks. Prefer a concrete component
    # intent when one is present, while retaining ambiguity for explicit
    # requests that join two different components (for example, "OSD và pool").
    if len(matches) > 1 and any(name == "cluster_health" for name, _ in matches):
        specific_matches = [match for match in matches if match[0] != "cluster_health"]
        if specific_matches:
            matches = specific_matches

    if not matches:
        return NaturalLanguageIntent(
            intent="unknown_or_ambiguous", language=language, original_text=original,
            normalized_text=folded, cluster_id=cluster_id, resource_type=resource_type,
            resource_ids=resource_ids, filters=filters, time_range=time_range,
            mode="read_only", confidence=0.35, needs_clarification=True,
            clarification_question=(
                "Bạn muốn xem health, OSD/PG, dung lượng, log hay backup của cụm?"
            ),
            decision_reason="no_intent_rule_matched",
        )

    intent, default_resource = matches[0]
    has_component_join = any(
        _contains(folded, conjunction) for conjunction in ("va", "hoac")
    )
    if len(matches) > 1 and resource_type:
        resource_matches = [match for match in matches if match[1] == resource_type]
        if len(resource_matches) == 1 and not has_component_join:
            matches = resource_matches
            intent, default_resource = matches[0]
    if len(matches) > 1 and intent not in {"explain_incident", "recommend_action"}:
        matched_names = ", ".join(name for name, _ in matches)
        return NaturalLanguageIntent(
            intent="unknown_or_ambiguous", language=language, original_text=original,
            normalized_text=folded, cluster_id=cluster_id, resource_type=resource_type,
            resource_ids=resource_ids, filters=filters, time_range=time_range,
            mode="read_only", confidence=0.62, needs_clarification=True,
            clarification_question=(
                f"Tôi nhận thấy nhiều phạm vi ({matched_names}). Bạn muốn ưu tiên phạm vi nào?"
            ),
            decision_reason="multiple_intent_rules_matched",
        )
    else:
        confidence = 0.84 if len(matches) > 1 else 0.9
    return NaturalLanguageIntent(
        intent=intent, language=language, original_text=original,
        normalized_text=folded, cluster_id=cluster_id,
        resource_type=resource_type or default_resource, resource_ids=resource_ids,
        filters=filters, time_range=time_range, mode="read_only",
        confidence=confidence,
        decision_reason=(
            f"priority_intent_rule:{intent}" if len(matches) > 1
            else f"intent_rule:{intent}"
        ),
    )


@lru_cache(maxsize=512)
def _route_cached(text: str, cluster_id: str | None) -> NaturalLanguageIntent:
    return _route_natural_language_uncached(text, cluster_id=cluster_id)


def route_natural_language(
    text: str,
    *,
    cluster_id: str | None = None,
) -> NaturalLanguageIntent:
    """Classify with a bounded cache while isolating mutable dict fields."""

    original = str(text or "").strip()
    normalized_cluster = str(cluster_id or "").strip() or None
    return deepcopy(_route_cached(original, normalized_cluster))
