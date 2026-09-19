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

| Area | Current implementation | Natural-language implication |
| --- | --- | --- |
| HTTP chat boundary | `dashboard/routes/chat.py`, `POST /api/chat/messages` | Preserve session, cluster scope, auth, and response shape. |
| Chat orchestration | `dashboard/chat_client.py::run_chat_turn` | Add classification as metadata/observability only in Phase 1. |
| Fixed Ceph reads | `dashboard/ceph_tools.py::FIXED_TOOL_COMMANDS` | Prefer these known read-only tools for future intent handlers. |
| Generic Ceph read path | `dashboard/ceph_tools.py::run_ceph_command_tool` | Existing denylist is not a complete allowlist; do not widen it. |
| Approval proposals | `TOOL_PROPOSE_ACTION`, `TOOL_PROPOSE_NODE_COMMAND` and route confirm endpoint | Natural language must never bypass preview, approval, or audit. |
| Evidence/citations | `_citations_from_result` and persisted `tools_used` | Keep evidence separate from an inferred answer in later phases. |
| Cluster selection | `cluster` passed into `run_chat_turn` | Parser receives `cluster_id`; it never infers or switches cluster. |

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
2. The provider currently performs intent interpretation implicitly. There is
   no persisted, provider-independent intent/entity object.
3. Natural-language requests do not yet have a deterministic clarification
   contract for ambiguous or mutation-like wording.
4. No benchmark numbers are claimed here. Baseline collection must use a
   staging/fake runner and must not execute management commands on a real
   cluster.

## Phase 1 contract

`shared.natural_language.route_natural_language()` is a pure function. It:

* returns a stable intent/entity schema;
* is read-only and has no SSH, subprocess, database, or provider dependency;
* carries the already-selected `cluster_id` without changing cluster scope;
* marks mutation-like requests for clarification instead of executing them;
* returns `unknown_or_ambiguous` when confidence is insufficient.

The current chat integration logs the classification only. It does not use the
classification to call tools, propose actions, or alter the chat response.

