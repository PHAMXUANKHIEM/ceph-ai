"""Natural-language understanding and bounded read-only query planning."""

from .query_executor import QueryExecutionResult, ToolEvidence, execute_query_plan
from .query_planner import PlannedToolCall, QueryPlan, plan_query
from .router import route_natural_language
from .schema import NaturalLanguageIntent, TimeRange
from .snapshot_runner import SnapshotQueryRunner
from .analyzers import ANALYZERS, Finding, analyze_evidence, analyze_evidence_bundle
from .retrieval import (
    KnowledgeCitation,
    KnowledgeIngestError,
    KnowledgeStore,
    RetrievalResult,
    build_default_knowledge_store,
)
from .rca import (
    RcaReport,
    build_read_only_rca,
    render_read_only_rca,
    validate_citation_ids,
    validate_read_only_rca,
)
from .conversation import (
    NaturalLanguageConversationState,
    conversation_state_from_intent,
    resolve_natural_language_turn,
)
from .action_planner import ActionPlanningError, ActionPreview, validate_action_preview
from .answer import (
    NaturalLanguageAnswer,
    NaturalLanguageAnswerError,
    parse_provider_answer,
    render_provider_answer,
    validate_provider_answer,
)
from .incident_bridge import IncidentCandidateSignal, build_incident_candidate_signals
from .nl_metrics import get_natural_language_metrics, record_natural_language_metric
from .mcp_adapter import McpAdapterError, ReadOnlyMcpAdapter
from .tool_registry import FIXED_READ_ONLY_TOOL_REGISTRY, ToolSpec

__all__ = [
    "FIXED_READ_ONLY_TOOL_REGISTRY",
    "ANALYZERS",
    "ActionPlanningError",
    "ActionPreview",
    "NaturalLanguageAnswer",
    "NaturalLanguageAnswerError",
    "IncidentCandidateSignal",
    "Finding",
    "KnowledgeCitation",
    "KnowledgeIngestError",
    "KnowledgeStore",
    "NaturalLanguageConversationState",
    "NaturalLanguageIntent",
    "PlannedToolCall",
    "QueryExecutionResult",
    "QueryPlan",
    "RetrievalResult",
    "RcaReport",
    "SnapshotQueryRunner",
    "TimeRange",
    "ToolEvidence",
    "ToolSpec",
    "execute_query_plan",
    "analyze_evidence",
    "analyze_evidence_bundle",
    "build_default_knowledge_store",
    "build_read_only_rca",
    "render_read_only_rca",
    "conversation_state_from_intent",
    "plan_query",
    "route_natural_language",
    "resolve_natural_language_turn",
    "validate_citation_ids",
    "validate_read_only_rca",
    "validate_action_preview",
    "parse_provider_answer",
    "render_provider_answer",
    "validate_provider_answer",
    "build_incident_candidate_signals",
    "get_natural_language_metrics",
    "record_natural_language_metric",
    "McpAdapterError",
    "ReadOnlyMcpAdapter",
]
