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
from .tool_registry import FIXED_READ_ONLY_TOOL_REGISTRY, ToolSpec

__all__ = [
    "FIXED_READ_ONLY_TOOL_REGISTRY",
    "ANALYZERS",
    "Finding",
    "KnowledgeCitation",
    "KnowledgeIngestError",
    "KnowledgeStore",
    "NaturalLanguageIntent",
    "PlannedToolCall",
    "QueryExecutionResult",
    "QueryPlan",
    "RetrievalResult",
    "SnapshotQueryRunner",
    "TimeRange",
    "ToolEvidence",
    "ToolSpec",
    "execute_query_plan",
    "analyze_evidence",
    "analyze_evidence_bundle",
    "build_default_knowledge_store",
    "plan_query",
    "route_natural_language",
]
