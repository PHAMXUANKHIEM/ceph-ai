# Natural Language Ceph — baseline report

Measured on 2026-09-19 from `tests/fixtures/nl_queries_vi.yaml` on the
staging parser path. The benchmark runs the deterministic parser only; it does
not call an LLM, SSH, Ceph, database, or mutation executor.

| Metric | Result |
| --- | ---: |
| Fixture cases | 100 |
| Samples per case | 25 |
| Parser version | `nl-v1` |
| Model version | `deterministic-rule-parser` |
| Intent accuracy | 100% |
| Language accuracy | 100% |
| Resource type accuracy | 100% |
| Entity ID accuracy | 100% |
| Clarification safety | 100% |
| Parser latency p50 | 0.0194 ms |
| Parser latency p95 | 0.0410 ms |

All 100 fixture cases passed. The latency numbers cover normalization and
intent routing only; they are not end-to-end Chat, snapshot, tool, retrieval,
or model latency. The fixture includes abbreviated, accent-free, typo-like,
ambiguous and approval-bypass safety language.
