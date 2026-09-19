# Kế hoạch nâng cấp Natural Language và AI Operations cho ceph-ai

**Ngày lập:** 2026-09-19  
**Trạng thái:** `[~]` Đang triển khai — Pha 0–2 đã có implementation nền tảng
**Phạm vi:** Ceph AIOps, Chat với AI, RCA, log intelligence và các workflow có preview/approval/audit  
**Mục tiêu:** Người vận hành có thể hỏi và yêu cầu bằng tiếng Việt tự nhiên; hệ thống hiểu đúng cluster, tài nguyên và khoảng thời gian, trả lời có bằng chứng, đồng thời không biến câu nói tự nhiên thành thao tác quản trị tự động.

## 1. Kết luận khảo sát repository

### 1.1 Repository tham khảo chính

1. [`croit/mcp-croit-ceph`](https://github.com/croit/mcp-croit-ceph)
   - Gần nhất với bài toán Ceph natural language.
   - Tham khảo: tool registry, field selection, filtering, smart summary, cache,
     drill-down, log search và phân quyền ADMIN/VIEWER.
   - Không tích hợp nguyên trạng vì phụ thuộc Croit REST API/OpenAPI và token của
     Croit. `ceph-ai` hiện truy cập Ceph qua collector, SSH/cephadm và API nội bộ.

2. [`k8sgpt-ai/k8sgpt`](https://github.com/k8sgpt-ai/k8sgpt)
   - Tham khảo mô hình deterministic analyzer trước, LLM giải thích sau.
   - Có custom analyzer và MCP server; có thể chuyển thành analyzer cho các mã
     lỗi Ceph như `OSD_DOWN`, `PG_DEGRADED`, `HEALTH_ERR`, `MON quorum` và
     `SLOW_OPS`.

3. [`GoogleCloudPlatform/kubectl-ai`](https://github.com/GoogleCloudPlatform/kubectl-ai)
   - Tham khảo intent → typed tool → preview/execute.
   - Hỗ trợ nhiều LLM provider và local provider như Ollama/llama.cpp.
   - Không dùng mô hình `LLM sinh shell command tự do`; chỉ áp dụng pattern tool
     allowlist hiện có của `ceph-ai`.

4. [`shreyanshjain7174/rook-ceph-mcp`](https://github.com/shreyanshjain7174/rook-ceph-mcp)
   - Chỉ tham khảo tool/prompt cho Rook Ceph trên Kubernetes.
   - Không phải dependency mặc định vì `ceph-ai` phải hỗ trợ cephadm/package và
     cluster không chạy qua Kubernetes.

5. [`schmitech/orbit`](https://github.com/schmitech/orbit)
   - Tham khảo thêm cho private RAG, log search và kết nối MCP/API/Elasticsearch.
   - Chưa đưa vào dependency chính; chỉ xem xét nếu cần một gateway RAG riêng.

### 1.2 Quyết định kiến trúc

- Không thay toàn bộ `dashboard/chat_client.py` bằng LangChain, LangGraph hay một
  agent framework mới.
- Giữ OpenAI-compatible tool loop hiện tại, `dashboard/ceph_tools.py`,
  `dashboard/routes/chat.py`, `shared/ai_*`, cơ chế session và approval/audit.
- Xây một lớp chuẩn hóa intent và tool schema nội bộ; có thể thêm MCP adapter ở
  sau nhưng MCP không được trở thành điều kiện để Chat hiện tại hoạt động.
- Luồng thay đổi dữ liệu bắt buộc giữ:

```text
Ngôn ngữ tự nhiên
    ↓
Intent + entities + cluster scope + time window
    ↓
Read-only query hoặc typed action
    ↓
Evidence / preview / risk classification
    ↓
User approval nếu cần
    ↓
Executor hiện tại
    ↓
Post-check + audit + trả kết quả
```

## 2. Nguyên tắc bắt buộc

### 2.1 Evidence-first

- Mọi câu trả lời về trạng thái Ceph phải gắn `cluster_id`, `collected_at`,
  `age_seconds`, nguồn dữ liệu và trạng thái `fresh/stale/partial`.
- Phân biệt ba loại nội dung:
  - `OBSERVED`: dữ liệu đọc được trực tiếp từ Ceph/cache.
  - `INFERRED`: suy luận từ nhiều evidence.
  - `RECOMMENDED`: đề xuất của AI.
- Nếu thiếu evidence hoặc snapshot quá cũ: trả `INSUFFICIENT_EVIDENCE`, không tự
  điền giá trị, không đoán nguyên nhân và không tạo action.
- Không gửi SSH key, secret key, access token, Bot Token, password hoặc raw
  credential vào model, log prompt hay audit output.

### 2.2 Tool-first, không shell tự do

- Model chỉ được gọi tool có tên, schema và target allowlist cố định.
- Tool read-only và tool mutation phải có namespace riêng.
- Không parse command từ văn bản AI để đưa thẳng vào executor.
- Mỗi tool có timeout, giới hạn output, giới hạn số dòng, pagination và
  `correlation_id`.

### 2.3 Cluster scope và phân quyền

- Mọi intent phải có cluster context; nếu người dùng nói “cụm này” thì dùng
  cluster đang chọn trong session.
- Nếu có nhiều cluster và câu hỏi mơ hồ, hỏi lại trước khi gọi Ceph.
- Không cho model tự đổi cluster scope.
- RBAC được kiểm tra lại ở server trước tool call; không tin vào vai trò do model
  tự sinh.

### 2.4 Safety

- `READ_ONLY`: được gọi ngay nếu đủ evidence.
- `SAFE`: vẫn tạo preview, có post-check.
- `RISKY`: bắt buộc approval hiện tại, Telegram approval nếu policy yêu cầu.
- `DESTRUCTIVE`: xác nhận riêng, nhập lại target/identifier nếu workflow đang yêu cầu.
- Không thay đổi chính sách approval, audit, authentication hoặc quyền hiện có.

## 3. Pha 0 — Inventory và baseline hiện trạng

**Mục tiêu:** biết chính xác năng lực hiện tại trước khi thêm lớp NLP.

### Công việc

- [ ] Đọc và lập bản đồ:
  - `dashboard/chat_client.py` — provider, tool loop, streaming, max iterations,
    redaction và lỗi provider.
  - `dashboard/ceph_tools.py` — tool read-only, action schema, target validation.
  - `dashboard/routes/chat.py` — session, cluster scope, approval và API contract.
  - `dashboard/static/chat_widget.js` — trạng thái loading/error/stop/preview.
  - `shared/ai_delegation.py` — routing intent và task delegation hiện có.
  - `watcher/log_analysis.py` — tool call và phân tích log hiện có.
  - `shared/ai_redaction.py`, `shared/ai_output.py`, `shared/ai_observability.py`.
- [ ] Liệt kê toàn bộ tool hiện có thành bảng:
  - tên tool;
  - read/write;
  - target và cluster scope;
  - timeout;
  - dữ liệu trả về;
  - evidence source;
  - quyền cần thiết;
  - có preview/approval/audit hay chưa.
- [ ] Ghi baseline 20–30 câu hỏi tiếng Việt thật của operator, gồm câu hỏi ngắn,
  câu thiếu chủ ngữ, từ viết tắt và lỗi chính tả thường gặp.
- [ ] Đo baseline:
  - intent accuracy;
  - đúng cluster/resource;
  - tool selection accuracy;
  - số tool call mỗi câu;
  - latency p50/p95;
  - tỷ lệ hallucinated command/action;
  - tỷ lệ yêu cầu cần hỏi lại.

### Deliverables

- [ ] `docs/ai/natural-language-tool-inventory.md`.
- [ ] `tests/fixtures/nl_queries_vi.yaml`.
- [ ] Báo cáo baseline JSON/Markdown có prompt version và model version.

### Gate

Không bắt đầu mở rộng tool nếu chưa biết tool nào đang có thể gọi trực tiếp và
tool nào đang bypass approval.

## 4. Pha 1 — Intent và entity schema cho tiếng Việt

**Mục tiêu:** biến câu tự nhiên thành cấu trúc có thể validate, không để model
quyết định bằng văn bản tự do.

### Schema dự kiến

```json
{
  "intent": "cluster_health",
  "language": "vi",
  "cluster_id": "...",
  "resource_type": "osd",
  "resource_ids": [],
  "filters": {"utilization_gt": 80},
  "time_range": {"duration": "1h"},
  "mode": "read_only",
  "confidence": 0.94,
  "needs_clarification": false,
  "clarification_question": null
}
```

### Intent v1

- [ ] `cluster_health` — “Cụm đang khỏe không?”, “Cụm lỗi gì?”.
- [ ] `osd_health` — OSD down, full, nearfull, outlier latency.
- [ ] `pg_health` — degraded, undersized, inactive, stuck.
- [ ] `pool_capacity` — pool đầy, còn bao nhiêu, top pool sử dụng.
- [ ] `node_metrics` — CPU/RAM/IOPS/latency theo node và khoảng thời gian.
- [ ] `rgw_diagnosis` — RGW error, S3 status, bucket/access log.
- [ ] `volume_insight` — volume stale, unattached, protection gap, performance.
- [ ] `crush_analysis` — tree, rule, skew, failure-domain risk.
- [ ] `log_search` — tìm log theo node/service/time/severity.
- [ ] `backup_status` — RPO/RTO, job lỗi, digest và protection.
- [ ] `explain_incident` — giải thích incident hiện có từ evidence.
- [ ] `recommend_action` — chỉ tạo recommendation/preview, không execute.
- [ ] `unknown_or_ambiguous` — hỏi lại hoặc hướng dẫn khả năng hiện có.

### Entity và normalization

- [ ] Chuẩn hóa tiếng Việt không dấu và viết tắt: `osd`, `mon`, `mgr`, `rgw`,
  `pg`, `pool`, `rbd`, `volume`, `bucket`, `cụm`, `node`, `máy`.
- [ ] Chuẩn hóa thời gian: “15 phút”, “1 giờ”, “từ hôm qua”, “7 ngày gần nhất”.
- [ ] Chuẩn hóa ngưỡng: “trên 80%”, “gần đầy”, “bị down”, “chậm”.
- [ ] Nhận diện IP, hostname, pool, volume ID, bucket, OSD ID và request ID.
- [ ] Không suy đoán entity khi có nhiều giá trị trùng; trả câu hỏi clarification.
- [ ] Lưu `parser_version`, `prompt_version`, `model`, `confidence` và lý do chọn
  tool để phục vụ đánh giá.

### Files dự kiến

- [x] `shared/natural_language/schema.py`.
- [x] `shared/natural_language/normalizer.py`.
- [x] `shared/natural_language/router.py` — deterministic intent router.
- [x] `tests/test_natural_language.py`.
- [ ] Bộ fixture chuẩn hóa 20–30 câu và đo accuracy chính thức.

### Gate

- Intent đúng tối thiểu 90% trên fixture đã duyệt.
- Cluster/resource extraction đúng tối thiểu 95% ở câu không mơ hồ.
- 100% câu mơ hồ nguy hiểm phải hỏi lại, không gọi mutation tool.

## 5. Pha 2 — Chuẩn hóa tool registry và query planner

**Mục tiêu:** học pattern tối ưu token của Croit nhưng dùng tool nội bộ của
`ceph-ai`.

### Công việc

- [x] Tạo metadata cho mỗi fixed read-only tool:
  - `tool_name`, `description_vi`, `input_schema`, `output_schema`;
  - `read_only`, `risk_level`, `required_role`;
  - `supported_deployment_modes`, `supported_ceph_versions`;
  - `max_output_items`, `timeout_seconds`, `cache_ttl`;
  - `evidence_fields`, `redaction_policy`.
- [x] Tách fixed tool thành nhóm:
  - `health_summary`;
  - `inventory_summary`;
  - `resource_detail`;
  - `logs_search`;
  - `analysis`;
  - `preview_action`;
  - `execute_approved_action`.
- [x] Query planner chọn dữ liệu nhỏ trước:
  - summary trước detail;
  - filter server-side trước khi serialize;
  - pagination/cursor cho OSD, PG, log và bucket lớn;
  - drill-down chỉ khi câu hỏi yêu cầu.
- [x] Dùng snapshot/cache hiện có thay vì gọi SSH trực tiếp từ Chat bằng
  `shared/natural_language/snapshot_runner.py`.
- [ ] Nếu snapshot stale, trả metadata stale và chỉ enqueue refresh khi policy cho
  phép; không chặn toàn bộ câu trả lời.
- [x] Giới hạn query plan: max calls, total duration, total output bytes và
  duplicate-call detection.

Implementation hiện tại:

* `shared/natural_language/tool_registry.py` — metadata và allowlist fixed tools.
* `shared/natural_language/query_planner.py` — intent → bounded read-only plan.
* `shared/natural_language/query_executor.py` — controlled parallel execution,
  timeout, partial evidence và aggregate output cap.
* `shared/natural_language/snapshot_runner.py` — đọc snapshot theo cluster,
  không có SSH fallback, trả freshness/partial/refreshing metadata.
* `config/settings.py` — `AI_NATURAL_LANGUAGE_QUERY_PLANNER_ENABLED`, mặc định
  tắt; `AI_NATURAL_LANGUAGE_SNAPSHOT_RUNNER_ENABLED` cũng mặc định tắt. Khi bật,
  snapshot evidence được đưa vào context model và vẫn fallback về chat loop cũ.

### MCP adapter tùy chọn

- [ ] Thiết kế adapter MCP read-only cho các tool summary/detail.
- [ ] Không đưa mutation tool vào MCP v1 nếu chưa có approval context.
- [ ] MCP request phải mang cluster scope server-side; không nhận cluster ID tùy ý
  từ prompt mà không validate.
- [ ] Chỉ bật adapter sau khi tool registry nội bộ đã ổn định.

### Gate

- [ ] Token/context giảm tối thiểu 50% so với đưa raw output toàn bộ vào prompt —
  chưa benchmark trên snapshot adapter.
- [x] Không có tool call trùng trong một query plan.
- [x] Planner không làm thay đổi API health/dashboard; feature flag mặc định tắt.

## 6. Pha 3 — Ceph deterministic analyzers theo pattern K8sGPT

**Mục tiêu:** phần phát hiện lỗi phải dựa trên rule/evidence; LLM chỉ diễn giải,
xếp hạng và giao tiếp.

### Analyzer v1

- [ ] `HealthAnalyzer`: `HEALTH_OK/WARN/ERR`, health detail và severity.
- [ ] `OSDAnalyzer`: down, out, nearfull, full, high latency, heartbeat/slow ops.
- [ ] `PGAnalyzer`: degraded, undersized, inactive, stale, stuck, recovery.
- [ ] `MONAnalyzer`: quorum, clock skew, unavailable MON.
- [ ] `PoolAnalyzer`: usage threshold, replica/EC, PG distribution.
- [ ] `RGWAnalyzer`: endpoint, S3 errors, auth, quota, multisite/sync.
- [ ] `NodeAnalyzer`: CPU, RAM, disk IOPS/latency, SSH reachability.
- [ ] `BackupAnalyzer`: RPO/RTO, failed jobs, stale metadata, restore drill.
- [ ] `CRUSHAnalyzer`: failure-domain concentration, skew, missing host/rule.

### Analyzer contract

```json
{
  "analyzer": "osd_health",
  "finding_id": "finding-...",
  "severity": "warning",
  "status": "OBSERVED",
  "summary": "OSD 12 đang nearfull",
  "entities": {"osd_id": 12, "host": "10.0.0.12"},
  "evidence": [],
  "confidence": 0.99,
  "next_checks": [],
  "recommended_action": null
}
```

- [ ] Finding phải deterministic và idempotent.
- [ ] Không cho analyzer sinh command shell.
- [ ] Có `evidence_gaps` khi thiếu dữ liệu.
- [ ] Có deduplication/debounce để không tạo lại cùng finding ở mỗi poll.
- [ ] Đưa findings vào incident/log intelligence hiện có khi phù hợp.

### Gate

- Mỗi finding có unit test dữ liệu đúng, thiếu, stale và sai schema.
- Không kết luận `HEALTH_OK` khi collector không có dữ liệu.
- Tỷ lệ false positive được đo trên fixture trước khi bật Telegram alert.

## 7. Pha 4 — RAG cho tài liệu Ceph và runbook nội bộ

**Mục tiêu:** trả lời “tại sao” và “làm thế nào” bằng tài liệu đúng phiên bản,
không biến RAG thành nguồn sự thật thay cho cluster evidence.

### Nguồn dữ liệu

- [ ] Tài liệu Ceph chính thức theo major release được hỗ trợ.
- [ ] Runbook nội bộ đã duyệt.
- [ ] Capability matrix của `ceph-ai`.
- [ ] RCA knowledge base hiện có: `docs/ceph-ai-rca-knowledge.md`.
- [ ] Incident/postmortem đã verified, có redaction.
- [ ] Không ingest raw secret, private key, access key hoặc audit payload nhạy cảm.

### Retrieval

- [ ] Metadata filter theo Ceph version, deployment mode, component và language.
- [ ] Hybrid retrieval: exact keyword cho mã lỗi + semantic retrieval cho câu hỏi
  tự nhiên.
- [ ] Reranking và giới hạn top-k.
- [ ] Mỗi đoạn trả về source, version, section và confidence.
- [ ] Nếu tài liệu khác version cluster, hiển thị cảnh báo và không dùng làm căn cứ
  duy nhất cho action.
- [ ] Hỗ trợ tiếng Việt bằng glossary/translation layer; không dịch sai command,
  flag, resource ID và log code.

### Storage lựa chọn

- [ ] Ưu tiên tận dụng database/cache hiện có trước khi thêm vector database.
- [ ] Chỉ thêm Qdrant/pgvector/FAISS nếu benchmark chứng minh cần thiết.
- [ ] Embedding/index phải có version, checksum và cách rebuild.

### Gate

- Câu trả lời có citation nội bộ cho runbook/tài liệu.
- RAG không được ghi đè evidence live từ Ceph.
- Test version mismatch phải fail closed.

## 8. Pha 5 — Natural-language RCA và hội thoại nhiều lượt

**Mục tiêu:** biến câu hỏi tiếng Việt thành báo cáo vận hành ngắn, có thể kiểm tra.

### Luồng trả lời

- [ ] Classify intent và scope.
- [ ] Chọn snapshot/evidence cần thiết.
- [ ] Chạy analyzer deterministic.
- [ ] Truy hồi runbook phù hợp.
- [ ] LLM tổng hợp theo output schema.
- [ ] Server validate facts và chặn citation không tồn tại.
- [ ] Trả lời theo các phần:
  - Kết luận ngắn.
  - Điều đã quan sát.
  - Bằng chứng và thời điểm.
  - Suy luận có confidence.
  - Việc nên kiểm tra tiếp.
  - Đề xuất hành động, nếu có.

### Conversation state

- [ ] Lưu cluster scope, time window, resources và unresolved clarification.
- [ ] Follow-up như “còn node kia thì sao?” phải dùng đúng context trước đó.
- [ ] Cho phép reset scope rõ ràng.
- [ ] Không để context từ cluster này rò sang cluster khác.
- [ ] Giới hạn lịch sử đưa vào model; summarize conversation khi dài.

### Vietnamese UX

- [ ] Mặc định trả lời tiếng Việt nếu user hỏi tiếng Việt.
- [ ] Giữ nguyên thuật ngữ Ceph: OSD, MON, MGR, PG, pool, RGW, CRUSH.
- [ ] Cho phép user yêu cầu English output.
- [ ] Hiển thị thuật ngữ chưa hiểu và hỏi lại thay vì âm thầm đoán.
- [ ] Có câu trả lời nhanh cho các intent phổ biến, không gọi LLM nếu summary cache
  đã đủ.

## 9. Pha 6 — Tích hợp action planner với approval hiện có

**Mục tiêu:** câu “hãy sửa/xóa/tăng/giảm” chỉ tạo proposal hợp lệ, không tự chạy.

### Công việc

- [ ] Intent router chỉ được map tới `action_id` đã đăng ký.
- [ ] Validate typed parameters: cluster, pool, image, OSD, PG, node và threshold.
- [ ] Preflight version/capability/health/dependency trước preview.
- [ ] Preview hiển thị:
  - action;
  - target;
  - command/operation description đã redacted;
  - risk;
  - expected impact;
  - rollback/limitation;
  - evidence timestamp;
  - expiry.
- [ ] RISKY/DESTRUCTIVE luôn giữ Telegram/user approval theo policy.
- [ ] Từ chối prompt injection kiểu “bỏ qua phê duyệt”, “chạy ngay”, “xóa xác nhận”.
- [ ] Post-check phải gọi snapshot mới hoặc kiểm chứng rõ ràng; không báo thành công
  chỉ vì SSH command exit 0.
- [ ] Audit lưu actor, prompt hash, parsed intent, action_id, preview, approval,
  execution, post-check và error đã redaction.

### Gate

- Test chứng minh model không thể gọi executor trực tiếp.
- Test action sai target, sai cluster, stale evidence, expired approval và duplicate
  approval đều bị chặn.

## 10. Pha 7 — Tối ưu hiệu năng và chi phí

- [ ] Fast path cho health/summary từ snapshot, không gọi LLM.
- [ ] Cache intent normalization và tool metadata.
- [ ] Cache RAG theo query/version/document revision.
- [ ] Gộp các read query độc lập và giới hạn concurrency.
- [ ] Tóm tắt dataset lớn trước khi đưa vào prompt.
- [ ] Model routing:
  - model nhỏ cho classify/normalize;
  - model nhanh cho summary;
  - model mạnh chỉ cho RCA phức tạp hoặc action planning.
- [ ] Timeout riêng cho intent, retrieval, Ceph tool và final answer.
- [ ] Cancel toàn bộ child task khi client disconnect.
- [ ] Metrics:
  - intent latency;
  - tool latency;
  - cache hit/miss;
  - prompt/response tokens;
  - RAG hit rate;
  - clarification rate;
  - approval rate;
  - hallucination/validation rejection;
  - cost per conversation.

### Mục tiêu ban đầu

- Fast read từ snapshot: p95 dưới 500 ms nếu không cần LLM.
- Intent classification: p95 dưới 2 giây.
- Câu hỏi summary có cache: p95 dưới 5 giây.
- Không request nào chờ vô hạn.
- Dataset lớn không làm prompt vượt budget.

## 11. Pha 8 — UI Chat và khả năng kiểm chứng

- [ ] Hiển thị trạng thái: `Đang hiểu câu hỏi`, `Đang lấy evidence`, `Đang phân tích`,
  `Cần phê duyệt`, `Đã hoàn tất`, `Thiếu dữ liệu`, `Lỗi kết nối`.
- [ ] Phân biệt bằng visual:
  - câu trả lời;
  - evidence;
  - inference;
  - recommendation;
  - preview;
  - execution result.
- [ ] Nút mở evidence detail: source, timestamp, stale/partial, tool đã gọi.
- [ ] Command/code block có copy nhưng không hiển thị secret.
- [ ] Nút Stop hủy request và tool task còn lại.
- [ ] Confirmation dialog/focus/accessibility không bị bypass bởi Chat.
- [ ] Câu hỏi gần đây chỉ lưu metadata an toàn, có cluster scope rõ.
- [ ] Không để Chat panel che KPI hoặc thao tác chính trên Dashboard.

## 12. Pha 9 — Bộ đánh giá và kiểm thử

### Dataset đánh giá

- [ ] 100 câu tiếng Việt thật, chia:
  - 30 câu health/inventory;
  - 20 câu log/RCA;
  - 15 câu capacity/performance;
  - 15 câu RGW/object storage;
  - 10 câu backup;
  - 10 câu action/approval.
- [ ] Mỗi câu có expected intent, entities, cluster, tools, answer facts và safety
  label.
- [ ] Thêm câu không dấu, viết tắt, typo, slang vận hành và câu mơ hồ.
- [ ] Thêm prompt injection và câu yêu cầu bypass approval.

### Test bắt buộc

- [ ] Intent classification và entity extraction.
- [ ] Multi-turn cluster scope.
- [ ] Version/deployment capability mismatch.
- [ ] Stale/partial snapshot.
- [ ] Node/RGW unreachable nhưng snapshot cũ còn.
- [ ] Tool timeout, output limit, duplicate tool call.
- [ ] Không leak secret trong prompt/log/response.
- [ ] Read-only tool không tạo mutation.
- [ ] Mutation chỉ tạo preview và approval.
- [ ] Client disconnect hủy task.
- [ ] RAG citation đúng source/version.
- [ ] Prompt injection không thay đổi policy.
- [ ] Multi-cluster không trộn evidence.

### Chỉ số nghiệm thu

- Intent accuracy ≥ 90% trên dataset đã duyệt.
- Resource/cluster extraction ≥ 95% ở câu không mơ hồ.
- 100% action RISKY/DESTRUCTIVE bị chặn nếu thiếu approval.
- 0 secret leak trong test redaction.
- 0 cross-cluster evidence contamination.
- 100% câu trả lời stale có nhãn stale.
- Tool error có message có cấu trúc, không traceback cho người dùng.

## 13. Pha 10 — Rollout và rollback

### Rollout

- [ ] Feature flag `NLP_INTENT_V1` chỉ bật cho admin/test cluster.
- [ ] Shadow mode: parse intent và chọn tool nhưng chưa thay đổi câu trả lời cũ.
- [ ] So sánh output cũ/mới trên fixture và log đã redaction.
- [ ] Canary cho một cluster, sau đó mở theo role.
- [ ] Theo dõi latency, cost, rejection và approval trong 24–72 giờ.
- [ ] Tài liệu hóa prompt/model/index version trong release manifest.

### Rollback

- [ ] Tắt feature flag để quay về Chat/tool loop hiện tại.
- [ ] RAG/index có thể xóa/rebuild mà không ảnh hưởng database nghiệp vụ.
- [ ] Không migration phá vỡ chat history hoặc audit hiện có.
- [ ] Không thay đổi command executor trong cùng release với NLP v1 nếu không bắt
  buộc.
- [ ] Giữ audit và evidence của shadow/canary để điều tra sau rollback.

## 14. File dự kiến thay đổi

### Giữ và mở rộng có kiểm soát

- `dashboard/chat_client.py`
- `dashboard/ceph_tools.py`
- `dashboard/routes/chat.py`
- `dashboard/static/chat_widget.js`
- `shared/ai_delegation.py`
- `shared/ai_output.py`
- `shared/ai_redaction.py`
- `shared/ai_observability.py`
- `watcher/log_analysis.py`

### Dự kiến tạo mới

- `shared/natural_language/schema.py`
- `shared/natural_language/normalizer.py`
- `shared/natural_language/intent_router.py`
- `shared/natural_language/clarification.py`
- `shared/natural_language/tool_registry.py`
- `shared/natural_language/query_planner.py`
- `shared/natural_language/analyzers/`
- `shared/natural_language/retrieval.py`
- `shared/natural_language/response_validator.py`
- `docs/ai/natural-language-tool-inventory.md`
- `docs/ai/natural-language-runbook.md`
- `tests/fixtures/nl_queries_vi.yaml`
- `tests/test_natural_language_*.py`

### Không được sửa tùy tiện

- authentication và RBAC;
- cluster scope middleware;
- approval/audit schema hiện tại;
- executor mutation;
- API contract cũ của Dashboard;
- watcher polling/event contract;
- secret storage và redaction policy.

## 15. Thứ tự thực hiện đề xuất

1. Pha 0 — Inventory và baseline.
2. Pha 1 — Intent/entity tiếng Việt, chỉ read-only.
3. Pha 2 — Tool registry/query planner và snapshot-first.
4. Pha 3 — Deterministic Ceph analyzers.
5. Pha 4 — RAG version-aware cho docs/runbook.
6. Pha 5 — RCA multi-turn và evidence response.
7. Pha 6 — Action planner nối vào preview/approval hiện có.
8. Pha 7 — Performance/cost.
9. Pha 8 — UI verification.
10. Pha 9 — Evaluation/security/chaos test.
11. Pha 10 — Shadow → canary → production rollout.

Không triển khai Pha 6 trước khi Pha 1–5 đạt gate. Không bật auto-remediation trong
Natural Language v1.

## 16. Tiêu chí hoàn thành toàn bộ kế hoạch

- [ ] Operator hỏi bằng tiếng Việt và hệ thống xác định đúng intent, cluster,
  resource và time window.
- [ ] Câu trả lời có evidence thật, timestamp, freshness và citation khi dùng RAG.
- [ ] Một node/RGW lỗi không làm mất snapshot hợp lệ trước đó.
- [ ] Dataset lớn được summary/filter/drill-down, không làm prompt phình to.
- [ ] RCA phân biệt observed/inferred/recommended.
- [ ] Mọi action thay đổi dữ liệu vẫn preview → approval → execute → post-check → audit.
- [ ] Prompt injection, sai cluster, stale evidence, unsupported version và thiếu
  permission đều fail closed.
- [ ] Có benchmark trước/sau về accuracy, latency, token và cost.
- [ ] Có feature flag, rollback và tài liệu vận hành.

## 17. Nhật ký triển khai

| Ngày | Pha | Thay đổi | Test/evidence | Người duyệt |
| --- | --- | --- | --- | --- |
| 2026-09-19 | 0 | Tạo kế hoạch, khảo sát repository tham khảo và kiến trúc AI hiện tại | Chưa triển khai code | — |
