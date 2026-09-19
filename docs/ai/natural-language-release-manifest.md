# Natural Language Ceph v1 release manifest

Release scope: deterministic Vietnamese intent routing, bounded read-only
snapshot planning, evidence-first RCA, validated provider output, action
preview/preflight/approval/audit integration, and the rollout UI.

## Versioned contracts

- parser: `nl-v1`
- query plan: `query-plan-v1`
- RCA: `rca-v1`
- provider answer: `answer-v1`
- chat context: `nl-context-v1`
- incident candidate signal: `incident-candidate-v1`
- retrieval index: manifest/checksum revision from the in-process knowledge
  store
- evaluation contract: `nl-evaluation-v1`
- shadow comparison: `nl-shadow-compare-v1` against base commit `55a510cd`
- benchmark report: `nl-before-after-v1` (parser-only, reference pricing)
- migration head: `m20260919nlcontext`

## Model and rollout policy

`ai_natural_language_small_model`, `ai_natural_language_fast_model`, and
`ai_natural_language_strong_model` are optional provider model ids. Empty
values preserve the provider's normal model. The small tier is used for
clarification, fast for ordinary summary, and strong for incident explanation,
recommendations, and complex RCA hints. Cost-routing remains same-provider,
allowlist/canary gated, and defaults to advisory/off behavior.

The NLP planner, snapshot runner, RAG, fast path, and structured provider
output are feature-flagged. Production defaults remain fail-closed:
admin-only rollout, no mutation from read-only analysis, and invalid provider
facts fall back to deterministic RCA.

The current canary is enabled only for `CS-LAB`
(`ac23b8ff-e235-414c-bed8-06894f3dedd3`) with planner, snapshot, RAG, and
structured output enabled; fast-path, snapshot refresh, and MCP remain off.
Non-admin access is accepted only for that explicit cluster allowlist. The
hourly `ceph-ai-natural-language-rollout-report.timer` records a redacted
24-hour window report for the required latency/cost/rejection/approval review.

## Evidence and rollback

AI provider telemetry is persisted in `AIInvocation` (latency, token usage,
provider/model, status and cost inputs). NLP-specific counters are exposed in
the admin-only `/api/debug/ceph-latency` response without prompt content.
Prompt hash, parsed intent, preview and approval state are included in the
existing append-only audit event for Chat actions.
Each persisted `nl_context` also records the redacted rollout scope
(`shadow`/`canary`/`admin`), active NLP feature flags, parser version, cluster
scope, and (when the snapshot runner executes) evidence references, freshness,
and citations. The allowlist and prompt/provider content are never persisted.

Rollback is configuration-only: disable NLP planner/snapshot/RAG/fast-path or
structured-output flags, then rebuild the lexical index if needed. The NLP
read path does not change the command executor or require deleting business
data. Do not enable canary model ids until their provider/model pair is
verified and allowlisted.

## Verification evidence

- 100 Vietnamese fixture cases: `tests/test_nl_fixture_accuracy.py`
- per-case expected contract: `tests/test_nl_evaluation_manifest.py`
- redacted old/new parser comparison: `tests/test_nl_shadow_comparison.py`
- before/after metrics: `tests/test_nl_before_after_benchmark.py`
- rollout report contract: `tests/test_nl_rollout_report.py`
- hourly report service: `ceph-ai-natural-language-rollout-report.timer`
- conditional Plan closeout: `scripts/close_nl_rollout_monitoring.py` (waits
  for `ready_for_close=true`; it cannot close the checklist early)
- full relevant NLP/Chat/Dashboard suite must pass before rollout
- `alembic current` must report `m20260919nlcontext (head)`
- Python compile, JavaScript syntax check, and `git diff --check` must pass
