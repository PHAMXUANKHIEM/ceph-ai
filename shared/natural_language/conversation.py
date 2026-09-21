"""Safe multi-turn context for natural-language Ceph requests."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping

from .router import route_natural_language
from .normalizer import fold_text
from .schema import NaturalLanguageIntent, TimeRange


_FOLLOW_UP_MARKERS = (
    "tai sao", "vi sao", "chi tiet", "tiep tuc", "con ", "them ",
    "nhu the nao", "dung khong", "cach nao", "nua",
)
_RESET_MARKERS = (
    "reset scope", "reset context", "bo qua context", "lam lai tu dau",
    "xoa context", "quay lai tu dau",
)


@dataclass(frozen=True)
class NaturalLanguageConversationState:
    """Serializable state that never chooses or changes a cluster."""

    cluster_id: str | None
    intent: str | None = None
    language: str = "unknown"
    resource_type: str | None = None
    resource_ids: tuple[str, ...] = ()
    time_range: TimeRange = TimeRange()
    unresolved_clarification: str | None = None
    parser_version: str = "nl-v1"

    def to_dict(self) -> dict[str, Any]:
        return {
            "cluster_id": self.cluster_id,
            "intent": self.intent,
            "language": self.language,
            "resource_type": self.resource_type,
            "resource_ids": list(self.resource_ids),
            "time_range": self.time_range.to_dict(),
            "unresolved_clarification": self.unresolved_clarification,
            "parser_version": self.parser_version,
        }


def _is_follow_up(text: str) -> bool:
    folded = fold_text(str(text or ""))
    return any(marker in folded for marker in _FOLLOW_UP_MARKERS)


def _is_reset(text: str) -> bool:
    folded = fold_text(str(text or ""))
    return any(marker in folded for marker in _RESET_MARKERS)


def _history_cluster(message: Mapping[str, Any]) -> str | None:
    value = message.get("cluster_id")
    return str(value).strip() if value else None


def _persisted_intent(message: Mapping[str, Any], *, cluster_id: str) -> NaturalLanguageIntent | None:
    """Read the already validated parser metadata persisted with a message."""

    context = message.get("nl_context")
    if not isinstance(context, Mapping):
        return None
    value = context.get("intent")
    if not isinstance(value, Mapping):
        return None
    if str(value.get("cluster_id") or "") != cluster_id:
        return None
    time_range_value = value.get("time_range")
    time_range = TimeRange(
        duration_seconds=(
            int(time_range_value["duration_seconds"])
            if isinstance(time_range_value, Mapping)
            and time_range_value.get("duration_seconds") is not None
            else None
        ),
        label=(
            str(time_range_value.get("label"))
            if isinstance(time_range_value, Mapping) and time_range_value.get("label")
            else None
        ),
    )
    return NaturalLanguageIntent(
        intent=str(value.get("intent") or "unknown_or_ambiguous"),
        language=str(value.get("language") or "unknown"),
        original_text=str(message.get("content") or ""),
        normalized_text=str(message.get("content") or ""),
        cluster_id=cluster_id,
        resource_type=(str(value["resource_type"]) if value.get("resource_type") else None),
        resource_ids=tuple(str(item) for item in value.get("resource_ids", ()) if str(item)),
        filters=dict(value.get("filters") or {}),
        time_range=time_range,
        mode=str(value.get("mode") or "read_only"),
        confidence=float(value.get("confidence") or 0.0),
        needs_clarification=bool(value.get("needs_clarification")),
        parser_version=str(value.get("parser_version") or "nl-v1"),
        decision_reason=(str(value["decision_reason"]) if value.get("decision_reason") else None),
    )


def _previous_intent(
    history: Iterable[Mapping[str, Any]],
    *,
    cluster_id: str | None,
) -> NaturalLanguageIntent | None:
    selected: NaturalLanguageIntent | None = None
    for message in history:
        if str(message.get("role") or "") != "user":
            continue
        recorded_cluster = _history_cluster(message)
        if not cluster_id or recorded_cluster != cluster_id:
            continue
        intent = _persisted_intent(message, cluster_id=cluster_id)
        if intent is None:
            intent = route_natural_language(str(message.get("content") or ""), cluster_id=cluster_id)
        if intent.intent != "unknown_or_ambiguous" and not intent.needs_clarification:
            selected = intent
    return selected


def conversation_state_from_intent(
    intent: NaturalLanguageIntent,
) -> NaturalLanguageConversationState:
    return NaturalLanguageConversationState(
        cluster_id=intent.cluster_id,
        intent=None if intent.intent == "unknown_or_ambiguous" else intent.intent,
        language=intent.language,
        resource_type=intent.resource_type,
        resource_ids=intent.resource_ids,
        time_range=intent.time_range,
        unresolved_clarification=intent.clarification_question if intent.needs_clarification else None,
        parser_version=intent.parser_version,
    )


def resolve_natural_language_turn(
    text: str,
    history: Iterable[Mapping[str, Any]],
    *,
    cluster_id: str | None,
) -> tuple[NaturalLanguageIntent, NaturalLanguageConversationState]:
    """Resolve one turn using same-cluster context only."""

    current = route_natural_language(text, cluster_id=cluster_id)
    previous = _previous_intent(history, cluster_id=cluster_id)
    if _is_reset(text):
        reset = replace(
            current,
            intent="unknown_or_ambiguous",
            resource_type=None,
            resource_ids=(),
            filters={},
            time_range=TimeRange(),
            needs_clarification=True,
            confidence=0.35,
            clarification_question="Đã reset context. Bạn muốn kiểm tra thành phần nào?",
            decision_reason="conversation_scope_reset",
        )
        return reset, conversation_state_from_intent(reset)
    if (
        previous is not None
        and current.intent == "unknown_or_ambiguous"
        and current.needs_clarification
        and _is_follow_up(text)
        and current.mode == "read_only"
    ):
        resolved = replace(
            current,
            intent=previous.intent,
            language=current.language if current.language != "unknown" else previous.language,
            resource_type=current.resource_type or previous.resource_type,
            resource_ids=current.resource_ids or previous.resource_ids,
            filters=current.filters or previous.filters,
            time_range=(
                current.time_range
                if current.time_range.duration_seconds is not None
                else previous.time_range
            ),
            confidence=min(0.82, previous.confidence),
            needs_clarification=False,
            clarification_question=None,
            decision_reason="follow_up_context_same_cluster",
        )
        return resolved, conversation_state_from_intent(resolved)
    return current, conversation_state_from_intent(current)
