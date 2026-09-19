# Natural Language Ceph — inventory and baseline

## Scope

This inventory is the Phase 0 boundary for natural-language work. It records
the current chat/tool path and the safety boundary that the first parser must
not cross. It does not change the existing approval, audit, or executor flow.

## Current request path

```text
Browser / Telegram
    -> dashboard/routes/chat.py
    -> dashboard/chat_client.py::run_chat_turn
    -> provider (Codex / Claude / OpenAI-compatible router)
    -> existing read-only tools or approval-gated proposal tools
    -> worker executor only after the existing confirmation path
```

## Module map

| Module | Responsibility in the current path |
| --- | --- |
| `dashboard/chat_client.py` | Provider loop, tool calls, redaction, and chat metadata. |
| `dashboard/ceph_tools.py` | Fixed read-only Ceph tools, target validation, and proposal boundaries. |
| `dashboard/routes/chat.py` | HTTP/session/cluster scope and confirmation API. |
| `dashboard/static/chat_widget.js` | Loading, error, stop, evidence, and preview UI states. |
| `shared/ai_delegation.py` | Existing task routing/delegation helpers. |
| `watcher/log_analysis.py` | Existing log collection and analysis path. |
| `shared/ai_redaction.py` | Secret and credential-like value redaction. |
| `shared/ai_output.py` | Structured AI output normalization and validation. |
| `shared/ai_observability.py` | AI request/tool metrics and audit-oriented observability. |
| `shared/natural_language/` | Provider-independent normalization, routing, planning, bounded execution, RCA rendering, conversation state, action-preview validation, and structured answer validation. |

| Area | Current implementation | Natural-language implication |
| --- | --- | --- |
| HTTP chat boundary | `dashboard/routes/chat.py`, `POST /api/chat/messages` | Preserve session, cluster scope, auth, and response shape. |
| Chat orchestration | `dashboard/chat_client.py::run_chat_turn` | Add classification as metadata/observability only in Phase 1. |
| Fixed Ceph reads | `dashboard/ceph_tools.py::FIXED_TOOL_COMMANDS` | Prefer these known read-only tools for future intent handlers. |
| Generic Ceph read path | `dashboard/ceph_tools.py::run_ceph_command_tool` | Existing denylist is not a complete allowlist; do not widen it. |
| Approval proposals | `TOOL_PROPOSE_ACTION`, `TOOL_PROPOSE_NODE_COMMAND` and route confirm endpoint | Natural language must never bypass preview, approval, or audit. |
| Evidence/citations | `_citations_from_result` and persisted `tools_used` | Keep evidence separate from an inferred answer in later phases. |
| Cluster selection | `cluster` passed into `run_chat_turn` | Parser receives `cluster_id`; it never infers or switches cluster. |

The first Phase 0/1 fixture uses the same boundary: routing receives an already
selected cluster and never opens a provider, SSH connection, subprocess, or
mutation path.

## Existing fixed read-only command inventory

The current fixed tool map contains these command families:

* `get_cluster_status` — `ceph status`
* `get_osd_stat` — `ceph osd stat`
* `get_osd_tree` — `ceph osd tree`
* `get_pool_list` — `ceph osd pool ls detail`
* `get_pg_stat` — `ceph pg stat`
* `get_df` — `ceph df`
* `get_health_detail` — `ceph health detail`
* `get_mon_stat` — `ceph mon stat`

This list is an inventory, not a promise that every tool is always available
for every user or cluster. The existing authorization and tool-schema checks
remain authoritative.

## Known gaps to address in later phases

1. The current generic `run_ceph_command` path uses a denylist rather than a
   closed read-only allowlist. It is a documented risk boundary and is not
   modified in Phase 1.
2. The provider still performs the production tool selection implicitly. The
   new `NaturalLanguageIntent` object is now available for shadow metadata, but
   it is not yet the production tool-selection authority.
3. Deterministic clarification now exists for unknown, multi-intent,
   mutation-like wording, and multiple same-type entity values.
4. Parser-only baseline numbers are recorded in
   `docs/ai/natural-language-baseline-report.*`; end-to-end Chat/tool/RAG
   latency and cost benchmarks remain open. Any baseline collection must use a
   staging/fake runner and must not execute management commands on a real
   cluster.

## Phase 1 contract

`shared.natural_language.route_natural_language()` is a pure function. It:

* returns a stable intent/entity schema;
* is read-only and has no SSH, subprocess, database, or provider dependency;
* carries the already-selected `cluster_id` without changing cluster scope;
* marks mutation-like requests for clarification instead of executing them;
* returns `unknown_or_ambiguous` when confidence is insufficient.

The deterministic parser also extracts IP/hostname, pool, volume, bucket,
OSD, PG, and request IDs; relative time windows; and utilization thresholds.
The 100-case Vietnamese fixture is at
`tests/fixtures/nl_queries_vi.yaml`, with a pytest accuracy gate in
`tests/test_nl_fixture_accuracy.py`.

The in-process lexical RAG index exposes a `lexical-v1` manifest with document
revisions, chunk count, and deterministic `index_revision` checksum. A changed
document makes the previous manifest stale; rebuilding is done by
`build_default_knowledge_store()`.

By default the chat integration logs classification only. When both
`AI_NATURAL_LANGUAGE_QUERY_PLANNER_ENABLED` and
`AI_NATURAL_LANGUAGE_SNAPSHOT_RUNNER_ENABLED` are enabled, it executes only the
bounded fixed read-only snapshot plan and includes deterministic analyzer
findings plus the validated `rca-v1` read-only report in the redacted evidence
context. If `AI_NATURAL_LANGUAGE_RAG_ENABLED=true`, it also adds bounded
runbook retrieval with citations as supporting context only. It still does not
propose or execute mutations. Retrieval results are cached by query, component,
language, and the current index revision, so an index rebuild naturally starts
a new cache namespace. For a canary/test cluster,
`AI_NATURAL_LANGUAGE_FAST_PATH_ENABLED=true` can return the validated
provider-free `rca-v1` rendering for supported read-only snapshot queries;
validation failures always fall back to the deterministic report. Structured
provider output is separately validated against that report. All planner/snapshot/RAG/fast-path
flags are admin-only by default; a non-admin canary requires
`ai_natural_language_admin_only=false` plus an explicit
`ai_natural_language_rollout_clusters` allowlist. Stale snapshot reads never
block the answer; `ai_natural_language_snapshot_refresh_enabled` separately
controls whether one deduplicated health refresh is queued in the background.

RCA reports expose incident candidates only as read-only, deduplicated
`incident-candidate-v1` signals. They do not insert an Incident or execute an
Action; the existing watcher/Log Intelligence pipeline remains the explicit
owner of persistence and correlation. Provider JSON is parsed as `answer-v1`
and server-validated against deterministic RCA facts, with deterministic
fallback on any unsupported fact, citation or cluster scope.

The release contract and rollback procedure are recorded in
`docs/ai/natural-language-release-manifest.md`.

The 100-case evaluation contract is frozen in
`docs/ai/nl-evaluation-manifest.json`; the same fixture is compared against
the pre-NLP `HEAD` parser in `docs/ai/nl-shadow-comparison.json`. The
comparison stores only redacted case text and structured classification fields.
Parser accuracy/latency/token/reference-cost before-after measurements are in
`docs/ai/nl-before-after-benchmark.json`; the report explicitly excludes
provider, SSH, Ceph command, database and mutation execution.

The repeatable rollout window report is
`scripts/report_natural_language_rollout.py`. It reads only persisted
content-free `nl_context`, provider telemetry, and chat approval audit events;
`--hours 24` or `--hours 72` can be used for the canary review without
printing prompts, responses, allowlists, or credentials.
The production host runs this report hourly through
`ceph-ai-natural-language-rollout-report.timer`, retaining the append-only
output in `/var/log/ceph-ai-natural-language-rollout.log` for rollback review.
Its `ExecStartPost` invokes `scripts/close_nl_rollout_monitoring.py`, which
updates the Plan checkbox only after the persisted monitoring gate reaches the
24-hour minimum; before that it records `status=waiting` and leaves the Plan
unchanged.

The read-only MCP boundary is implemented by `ReadOnlyMcpAdapter` but remains
disabled by default. It accepts only fixed registry tools and enforces the
server-selected cluster scope. The official release catalog is recorded in
`docs/ai/ceph-official-docs-manifest.md`; retrieval is version-filtered and
does not treat those documents as live cluster evidence.
