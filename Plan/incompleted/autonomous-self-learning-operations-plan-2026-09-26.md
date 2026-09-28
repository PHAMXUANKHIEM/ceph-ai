# Autonomous Self-Learning Operations Plan — Ceph AI

**Ngày lập:** 26/09/2026
**Trạng thái:** `NOT-STARTED` (plan chi tiết, chưa có hạng mục nào được triển khai)
**Liên quan:** `Plan/incompleted/strict-production-readiness-closure-plan-2026-09-25.md` (§7 AI/online learning, §5.3 live read-only, §6 safety)
**Mục tiêu:** đưa Ceph AI từ "đề xuất `investigate_manually` rồi bị từ chối" sang "tự điều tra, tự học từ nhãn thật, tự xử lý trong ngân sách rủi ro đã duyệt" — theo đúng thứ tự: **giảm nhiễu → có nhãn → có bằng chứng → có quyết định an toàn**.

---

## 0. Nguyên tắc bắt buộc

1. **Nhãn trước, thuật toán sau.** Không thêm model/bandit/RL nào trước khi có ≥ 100 nhãn độc lập cho fault family đó.
2. **Nhãn operator là ground truth duy nhất để mở quyền tự động.** Nhãn tự động (weak supervision) chỉ dùng cho đánh giá, xếp hạng, shadow — không bao giờ tự nâng quyền.
3. **Điều tra chỉ-đọc được phép tự động; hành động ghi luôn qua typed gateway** (`worker/executor/action_contract.py`), guardrail (`shared/autopilot_guardrails.py`), Trust Engine (`shared/trust_engine.py`), kill switch và rollback.
4. **Mọi thay đổi ngưỡng/cấu hình Ceph là action RISKY cần duyệt**, kể cả khi mục đích là giảm nhiễu.
5. **Đo trước khi đổi:** mỗi work package có baseline, chỉ số mục tiêu và cách đo bằng script đọc DB, không dùng cảm nhận.
6. **Không phá hợp đồng hiện có:** không hạ quyền Single Full, không tắt audit, không đổi schema mà thiếu migration + test migration.
7. **Mỗi hạng mục = commit riêng + test riêng + cập nhật bảng nhật ký §12.** Static-analysis budget và coverage gate phải pass.

## 1. Baseline hiện trạng (đo 26/09/2026, 30 ngày, đọc DB production)

| Chỉ số | Giá trị | Nguồn |
|---|---:|---|
| Incident / 30 ngày | 5.982 | `incidents` |
| `BLUESTORE_SLOW_OP_ALERT` | 1.040 | top code |
| `NODE_UNREACHABLE:*` (4 host) | 429 / 385 / 292 / 270 | top code |
| `OSD_LATENCY_HIGH:{3,4,5}` | 294 / 248 / 201 | top code |
| Remediation case | 3.320 (2.822 PROPOSED, 491 REJECTED) | `remediation_cases` |
| `VERIFIED_SUCCESS` | **1** | `remediation_cases.outcome` |
| Operator verdict | **0 / 3.320** | `remediation_cases.operator_verdict` |
| Action `investigate_manually` | 3.246 (≈ 97%), toàn bộ REJECTED | `actions` |
| Online learning: mẫu đã học | 0 (lỗi tz đã sửa `0670de30`, chờ deploy) | `online_learner_audit` |
| River v2 verified outcome | 4 (KEEP_SHADOW) | `scripts/river_v2_promotion_evidence.py` |

**Chẩn đoán:** hạ tầng học đã có (Case Memory, Trust Engine Wilson lower bound, Autopilot 3 mức, state machine, rollback planner, change-risk, model registry có promotion audit) nhưng **không có nhãn** và **quá nhiều nhiễu lặp**. Nguyên nhân nhiễu đã xác định trong code:

- `watcher/node_health_monitor.py::create_or_resolve_node_unreachable_incidents` đóng incident ngay khi **một** lần quét SSH thành công, trong khi mở lại chỉ cần `node_reachability_consecutive_failures=2` lần lỗi với chu kỳ 120s → host chập chờn sinh hàng trăm incident.
- `BLUESTORE_SLOW_OP_ALERT` (Ceph 18.2.5+/19.2.1+) mặc định `bluestore_slow_ops_warn_threshold=1`, `lifetime=86400` — quá nhạy với HDD.
- `investigate_manually` là action mặc định của `NODE_UNREACHABLE` (`NODE_UNREACHABLE_ACTION_ID`) và là fallback của router khi thiếu bằng chứng (`worker/llm/router_client.py` ~886, ~984).

## 2. Chỉ số mục tiêu tổng (KPI)

| KPI | Baseline | Mốc 1 (sau WP1–WP2) | Mốc 2 (sau WP3–WP4) | Mốc 3 (sau WP6) |
|---|---:|---:|---:|---:|
| Incident / tuần | ~1.400 | ≤ 280 (÷5) | ≤ 200 | ≤ 150 |
| Tỉ lệ `investigate_manually` | 97% | ≤ 80% | ≤ 40% | ≤ 25% |
| Operator verdict / tuần | 0 | ≥ 30 | ≥ 50 | ≥ 50 |
| Case có nhãn (operator) tích lũy | 0 | ≥ 60 | ≥ 200 | ≥ 400 |
| Evidence tự động đính kèm | 0% | — | ≥ 90% incident top-10 code | ≥ 95% |
| False remediation rate (FRR) đo được | n/a | n/a | n/a | ≤ ngân sách operator (đề xuất 5%) |
| Playbook đạt Trust Engine (≥20 mẫu, trust ≥0.85) | 0 | 0 | ≥ 1 | ≥ 3 |

Tất cả KPI được tính bởi `scripts/autonomy_kpi_report.py` (WP0) và hiển thị trên trang AI Learning.

---

## WP0 — Đo lường nền và KPI (tiền đề cho mọi WP)

**Mục tiêu:** một lệnh duy nhất trả toàn bộ KPI §2 theo tuần, theo cluster, theo fault family — đọc-only.

- [x] Tạo `scripts/autonomy_kpi_report.py` + `shared/autonomy_kpi.py` (read-only, ORM portable, chỉ đọc cột cần — 1,9s trên production) (`WP0 commit`):
  - incident/tuần theo `ceph_code` (chuẩn hóa `NODE_UNREACHABLE:*`, `OSD_LATENCY_HIGH:*` về family),
  - tỉ lệ incident "tái mở" (cùng code/host mở lại < 30 phút sau khi RESOLVED),
  - phân bố `actions.action_id`, tỉ lệ `investigate_manually`,
  - verdict/tuần theo loại, độ trễ verdict (detected → verdict),
  - evidence coverage (sau WP3), FRR (sau WP6),
  - `--since`, `--cluster`, `--weeks N`.
- [x] `tests/test_autonomy_kpi.py`: family, tái mở cùng code/khác host/ngoài cửa sổ, tỉ lệ placeholder, verdict + median, lọc cluster + legacy NULL, cửa sổ rỗng trả null, API admin.
- [x] `GET /api/ai-learning/autonomy-kpi?days=` (admin, theo cluster đang chọn, gồm dòng legacy NULL của cluster mặc định); thẻ KPI ở tab Tổng quan (`dashboard/static/autonomy_kpi.js`).
- [x] Baseline `docs/benchmark/autonomy-kpi-baseline-2026-09-28.json`: 6.817 incident/30 ngày (~1.591/tuần), **38,3% mở lại < 30 phút**, `investigate_manually` 97,5%, 0/4.155 verdict.

**Nghiệm thu:** script chạy trên DB production < 30s, số liệu khớp bảng §1 (sai lệch < 1%).
**Ước lượng:** 0,5–1 ngày.

---

## WP1 — Giảm nhiễu (ưu tiên P0)

### WP1.1 `NODE_UNREACHABLE`: hysteresis hồi phục + phát hiện flapping

**Hiện trạng:** mở sau 2 lần lỗi, đóng sau 1 lần OK → flapping sinh incident mới liên tục.

- [x] Thêm settings `node_reachability_recovery_successes=3`, `node_reachability_flap_window_seconds=3600`, `node_reachability_flap_threshold=3` (`config/settings.py`):
  - `node_reachability_recovery_successes: int = Field(default=3, ge=1, le=20)` — số lần quét OK liên tiếp trước khi RESOLVED,
  - `node_reachability_flap_window_seconds: int = 3600`,
  - `node_reachability_flap_threshold: int = 3` — số lần chuyển trạng thái trong cửa sổ để coi là flapping.
- [x] ~~Bảng trạng thái mới~~ không cần: số lần flapping đếm từ bảng `incidents` trong cửa sổ (bền qua restart); bộ đếm OK liên tiếp trong RAM, restart chỉ làm incident đóng theo hướng bình thường (có test).
- [x] Sửa `check_node_reachability` (hysteresis) + `create_or_resolve_node_unreachable_incidents` (giữ mở khi flapping, gắn `flapping`, 1 cảnh báo qua `telegram_outbox.enqueue_node_flapping_alert`):
  - chỉ RESOLVED khi `consecutive_ok >= recovery_successes`,
  - nếu `transitions` trong cửa sổ ≥ `flap_threshold` → **không mở incident mới**, cập nhật incident hiện có sang trạng thái/nhãn `FLAPPING` (ghi `signal_evidence_json.flapping=true`, số lần chuyển, khoảng thời gian),
  - Telegram: một thông báo "host chập chờn" mỗi cửa sổ, không phải mỗi lần.
- [x] Không đổi action mặc định (vẫn approval-gated).
- [x] Test trong `tests/test_node_health_monitor.py` (4 test mới, 18 test cũ giữ nguyên):
  - 1 lần OK giữa chuỗi lỗi không đóng incident,
  - 3 lần OK liên tiếp đóng incident,
  - 3 lần chuyển trạng thái trong 1 giờ → 1 incident FLAPPING, không có incident thứ hai,
  - Watcher restart giữa chừng giữ nguyên bộ đếm,
  - host khác không bị ảnh hưởng.
- [x] Replay 30 ngày: `NODE_UNREACHABLE` 1.992 → 231 (**−88,4%**), host nặng nhất 609 → 54.

**Nghiệm thu:** replay cho thấy `NODE_UNREACHABLE` giảm ≥ 80%; sau deploy 7 ngày đo bằng WP0 khớp replay ± 20%.
**Rollback:** đặt `recovery_successes=1`, `flap_threshold` rất lớn → hành vi cũ.
**Ước lượng:** 1–1,5 ngày.

### WP1.2 `BLUESTORE_SLOW_OP_ALERT`: từ incident lặp sang tín hiệu xu hướng

**Hiện trạng:** 1.040 incident/30 ngày; router có nhánh self-heal restart OSD có điều kiện (`router_client.py` ~1204).

- [ ] Collector đọc `ceph health detail` → trích `osd.N` + số slow op; lưu chuỗi thời gian theo OSD (tận dụng bảng metric hiện có hoặc bảng `bluestore_slow_op_samples`).
- [ ] Quy tắc mở incident mới (thay vì mỗi lần health check xuất hiện):
  - chỉ mở khi slow op/OSD vượt `P95 lịch sử × k` **hoặc** kéo dài > `T` phút **hoặc** ≥ 2 OSD cùng host,
  - nếu không → cập nhật "xu hướng" trên incident mở sẵn hoặc chỉ lưu metric.
- [ ] Action mới **approval-required, RISKY**: `tune_bluestore_slow_ops_warn` (đặt `bluestore_slow_ops_warn_threshold`/`lifetime` theo device class HDD/SSD bằng `ceph config set osd/class:hdd ...`), có preflight đọc giá trị hiện tại, rollback về giá trị cũ, audit. Đăng ký trong `worker/policy/action_policy.yaml`, `worker/executor/commands.py`, typed contract.
- [ ] Liên kết với WP3: mỗi incident BlueStore tự thu `dump_historic_slow_ops` và SMART của đĩa liên quan.
- [ ] Test: ngưỡng xu hướng, nhiều OSD cùng host, contract action (params hợp lệ/không hợp lệ), rollback plan.

**Nghiệm thu:** incident BlueStore giảm ≥ 70% mà không bỏ sót trường hợp slow op tăng đột biến (test replay với 3 đợt thật trong dữ liệu).
**Ước lượng:** 2 ngày.

### WP1.3 `OSD_LATENCY_HIGH:N` và health check tái mở

- [ ] Áp dụng cùng khung hysteresis (WP1.1) cho `OSD_LATENCY_HIGH:*`: mở sau N mẫu vượt ngưỡng, đóng sau M mẫu bình thường; ngưỡng theo device class.
- [ ] Chính sách chung "reopen suppression": incident cùng `dedupe_key` được RESOLVED < 30 phút trước → mở lại incident cũ (`REOPENED`, tăng `reopen_count`) thay vì tạo mới. Sửa ở điểm tạo incident chung của Watcher (dedupe hiện có trong `watcher/incident_grouping.py`).
- [ ] Test: reopen trong/ngoài cửa sổ, khác cluster không gộp, audit ghi `REOPENED`.

**Nghiệm thu:** tỉ lệ tái mở (WP0) giảm ≥ 60%.
**Ước lượng:** 1,5 ngày.

---

## WP2 — Thu nhãn (ưu tiên P0)

### WP2.1 Verdict một chạm trên Telegram

**Hiện trạng:** endpoint `POST /incidents/{id}/cases/{case_id}/verdict` (`dashboard/routes/incidents.py:592`) yêu cầu Dashboard; verdict xấu cần ghi chú ≥ 5 ký tự → không ai dùng.

- [ ] Callback mới trong `dashboard/telegram_approval_bot.py`: prefix `verdict:<case_id>:<VERDICT>[:<reason_code>]` (giữ trong giới hạn 64 byte callback_data của Telegram — dùng mã ngắn: `v:<case8>:C`, map lại qua DB).
- [ ] Bàn phím dưới tin nhắn chẩn đoán/incident:
  - hàng 1: `✅ Đúng` `❌ Sai` `⚠️ Nguy hiểm` `🤷 Chưa rõ`,
  - khi bấm `❌`/`⚠️`: sửa tin nhắn hiển thị **lý do chọn sẵn** (đáp ứng yêu cầu ghi chú): "Cảnh báo sai", "Chẩn đoán sai nguyên nhân", "Hành động không cần thiết", "Hành động có thể gây hại", "Không khắc phục được" → map sang `FALSE_POSITIVE`/`INEFFECTIVE`/`UNSAFE` + `operator_note` = lý do.
- [ ] Tái dùng kiểm soát người được phép (`TELEGRAM_APPROVAL_USER_IDS`, `_sender_may_decide`) + chat trust hiện có; ghi `operator_verdict_by = telegram:<id>`.
- [ ] Idempotent: verdict mới ghi đè có audit `EVENT_REMEDIATION_CASE_VERDICT_UPDATED` với giá trị cũ/mới; không cho verdict case của cluster khác.
- [ ] Refactor phần ghi verdict thành hàm dùng chung `shared/remediation_cases.record_verdict(...)` cho cả Dashboard và Telegram.
- [ ] Test: mọi nút, lý do bắt buộc cho verdict xấu, người ngoài allowlist bị chặn, callback_data ≤ 64 byte, cross-cluster bị chặn, audit.

**Nghiệm thu:** ≥ 30 verdict/tuần trong 2 tuần đầu (đo WP0).
**Ước lượng:** 1,5 ngày.

### WP2.2 Nhắc đánh giá có chủ đích

- [ ] Job hằng ngày (worker scheduler hiện có) chọn **tối đa 10 case đáng nhãn nhất**: ưu tiên fault family ít nhãn, case có `outcome` đã rõ (VERIFIED_*/EXECUTION_FAILED), case có AI confidence cao nhưng bị REJECTED (bất đồng người–máy).
- [ ] Gửi 1 tin Telegram "10 case cần anh/chị đánh giá" kèm nút WP2.1.
- [ ] Test chọn mẫu (active-learning heuristic) và giới hạn số lượng.

**Ước lượng:** 1 ngày.

### WP2.3 Nhãn tự động kiểu weak supervision (không cấp quyền)

- [ ] Module `shared/auto_labels.py` với các labeling function (LF) thuần, trả `CORRECT | FALSE_POSITIVE | INEFFECTIVE | ABSTAIN` + độ tin:
  - `lf_self_resolved_no_action`: incident RESOLVED trong ≤ N phút, không có action EXECUTED → "không cần hành động" (nhãn cho abstention),
  - `lf_postcheck_pass_no_regression`: outcome `VERIFIED_SUCCESS` và `regressed_24h=false` → CORRECT,
  - `lf_regressed`: `regressed_1h`/`regressed_24h=true` → INEFFECTIVE,
  - `lf_rejected_then_self_resolved`: action bị REJECTED và incident tự hết → đề xuất có thể thừa,
  - `lf_flapping_host`: incident thuộc chuỗi FLAPPING (WP1.1) → FALSE_POSITIVE cho đề xuất khởi động lại host,
  - `lf_same_code_reopened_after_action` → INEFFECTIVE.
- [ ] Bảng `remediation_case_auto_labels(case_id, lf_name, label, confidence, created_at)` + migration; bảng tổng hợp theo đa số có trọng số (ban đầu trọng số = precision LF đo trên nhãn operator).
- [ ] **Tuyệt đối không** ghi vào `operator_verdict`, không dùng trong `trust_engine` để mở autopilot; chỉ dùng cho: `ai_evaluation`, xếp hạng case cần nhãn (WP2.2), shadow bandit (WP6).
- [ ] Báo cáo chất lượng LF: coverage, conflict, precision so với nhãn operator (khi có ≥ 20 nhãn chồng lấp).
- [ ] Test từng LF + tổng hợp + đảm bảo không đụng `operator_verdict`.

**Nghiệm thu:** ≥ 60% case trong 30 ngày có ít nhất 1 nhãn tự động; precision LF được báo cáo.
**Ước lượng:** 2 ngày.

### WP2.4 Trang đánh giá nhanh trên Dashboard

- [ ] Trong Alert Center (`/alerts`), thêm cột verdict và thao tác nhanh (dùng chung `record_verdict`), lọc "chưa có nhãn".
- [ ] Browser acceptance (`scripts/browser_acceptance.mjs`) thêm check trang này.

**Ước lượng:** 1 ngày.

---

## WP3 — Điều tra tự động chỉ-đọc (evidence trước chẩn đoán) (ưu tiên P1)

**Mục tiêu:** thay `investigate_manually` bằng bộ bằng chứng thu tự động, không cần duyệt vì chỉ đọc.

### WP3.1 Registry evidence collector

- [ ] `shared/evidence_collectors.py`: mỗi collector = id, mô tả, lệnh typed (không chuỗi tự do), phạm vi (cluster/host/osd), timeout, kích thước tối đa, redaction. Ứng viên:
  - Ceph: `ceph osd perf`, `ceph osd tree`, `ceph pg dump_stuck`, `ceph device ls`, `ceph device get-health-metrics <devid>`, `ceph daemon osd.N dump_historic_slow_ops` (qua cephadm/docker exec), `ceph osd metadata N`,
  - Host (SSH read-only identity): `smartctl -a /dev/X`, `iostat -x 1 3`, `uptime`, `free -m`, `dmesg --since` (lọc), `journalctl -u ceph-osd@N --since -30min -n 200`, `ip -s link`, `ping -c 3` từ MON tới host.
- [ ] Kiểm soát read-only bằng allowlist + test giống `scripts/live_readonly_acceptance.py` (chặn mutation verb ở transport).
- [ ] Giới hạn: đồng thời ≤ 2 collector/host, ≤ 1 lượt/incident/10 phút, tổng thời gian ≤ 60s/incident, circuit breaker theo host.
- [ ] Test: allowlist, redaction, timeout, circuit breaker, không lệnh nào ghi.

### WP3.2 Runbook điều tra dạng dữ liệu

- [ ] `worker/policy/investigation_runbooks.yaml`: theo `fault_family`/code → danh sách collector + câu hỏi chẩn đoán + tiêu chí kết luận (giống cách HolmesGPT dùng runbook).
- [ ] Viết runbook cho top code: `NODE_UNREACHABLE`, `BLUESTORE_SLOW_OP_ALERT`, `OSD_LATENCY_HIGH`, `OSD_DOWN`, `MON_CLOCK_SKEW`, `PG_DEGRADED`, `POOL_NEARFULL`, `LARGE_OMAP_OBJECTS`, `DEVICE_HEALTH`, `SLOW_OPS`.
- [ ] Validator schema + test (mọi collector trong runbook phải tồn tại trong registry).

### WP3.3 Chạy tự động khi incident mở

- [ ] Hook trong Watcher/Worker: incident mới (không phải FLAPPING lặp) → chạy runbook → lưu `incident_evidence(incident_id, collector_id, status, output_redacted, started_at, duration_ms)` + migration.
- [ ] Hiển thị evidence trên timeline incident và Telegram (tóm tắt 3 dòng).
- [ ] Test end-to-end với client Ceph/SSH giả.

### WP3.4 Chẩn đoán xác định trước LLM

- [ ] `shared/deterministic_triage.py`: luật cho top code dựa trên evidence, ví dụ `NODE_UNREACHABLE`:
  - ping OK + SSH fail → "sshd/firewall", đề xuất kiểm tra dịch vụ SSH,
  - ping fail + OSD trên host down → "host down", đề xuất kiểm tra IPMI/nguồn,
  - ping fail + OSD vẫn up → "mạng quản trị lỗi, data plane ổn", **không** đề xuất reboot.
- [ ] LLM chỉ được gọi khi triage trả `UNKNOWN`; prompt chứa evidence thật + verified case (`shared/case_retrieval.py`).
- [ ] LLM phải trích evidence cho mỗi khẳng định; không có evidence → abstain (đo bằng `ai_evaluation` hallucination rate).
- [ ] Test luật + test prompt không gọi LLM khi triage đủ.

**Nghiệm thu WP3:** ≥ 90% incident top-10 code có evidence trong ≤ 2 phút; `investigate_manually` ≤ 40%; không có lệnh ghi nào trong audit của collector.
**Ước lượng:** 5–7 ngày.

---

## WP4 — Tận dụng tính năng sẵn có của Ceph (ưu tiên P1)

- [ ] Phát hiện trạng thái module mgr (`ceph mgr module ls`): `devicehealth`, `diskprediction_local`, `pg_autoscaler`, `balancer` — read-only, hiển thị trên Capability Matrix (`dashboard/routes/capability_matrix.py`).
- [ ] Action approval-required `enable_mgr_module` (chỉ cho `diskprediction_local`, `devicehealth`) với preflight + rollback (`disable`).
- [ ] Ingest `ceph device ls` + `ceph device predict-life-expectancy <devid>` định kỳ vào trang Disk Risk (`dashboard/routes/disk_risk.py`); dùng làm feature/evidence cho WP3 và WP6.
- [ ] `pg_autoscaler`/`balancer`: chỉ đọc trạng thái + khuyến nghị (`ceph osd pool autoscale-status`, `ceph balancer status/eval`), không tự bật.
- [ ] Test parser cho từng output JSON (fixture thật đã redact).

**Nghiệm thu:** Disk Risk hiển thị life expectancy cho ≥ 80% device khi module bật; mỗi incident `DEVICE_HEALTH` có dự đoán kèm theo.
**Ước lượng:** 2–3 ngày.

---

## WP5 — Nhãn từ diễn tập lỗi có kiểm soát (ưu tiên P2, cần staging)

- [ ] Catalogue kịch bản `scripts/chaos/scenarios.yaml`: stop 1 OSD, chặn mạng quản trị 1 node, clock skew MON, pool gần đầy (pool thử), slow disk giả lập (`tc`/`dm-delay` trên node test).
- [ ] Runner `scripts/chaos/run_scenario.py`: chỉ chạy với cluster id trong allowlist staging + token xác nhận + cửa sổ thời gian; ghi ground truth (scenario, thời điểm, mục tiêu), tự hoàn tác sau T phút, audit.
- [ ] Liên kết với trang `synthetic_incidents` hiện có (hiển thị run, cleanup).
- [ ] Sau mỗi run: chấm chẩn đoán/đề xuất bằng `shared/ai_evaluation.py` (precision, ECE, hallucination) theo scenario.
- [ ] Test: guard staging-only, hoàn tác bắt buộc, không chạy khi cluster production.

**Nghiệm thu:** ≥ 5 kịch bản × ≥ 10 lần chạy → ≥ 50 nhãn ground truth; báo cáo chất lượng chẩn đoán theo kịch bản.
**Ước lượng:** 4–5 ngày + thời gian chạy.

---

## WP6 — Quyết định tự động có ngân sách rủi ro (ưu tiên P2, chỉ khi đủ nhãn)

**Điều kiện bắt đầu:** ≥ 200 case có nhãn operator tổng, ≥ 50 cho fault family mục tiêu.

### WP6.1 Decision log phục vụ học off-policy

- [ ] Bảng `autonomy_decisions(id, case_id, context_features_json, candidate_actions_json, chosen_action, chosen_by{policy,operator}, propensity, policy_version, outcome, reward, created_at)` + migration.
- [ ] Ghi mọi quyết định hiện tại (kể cả đề xuất bị từ chối) với propensity: rule-based → 1.0 cho action được chọn; khi có exploration → xác suất thật.
- [ ] Feature: fault family, severity, evidence tóm tắt (WP3), số OSD/host ảnh hưởng, trạng thái cluster (degraded %, recovery), giờ trong ngày, lịch sử playbook (trust score), device class.

### WP6.2 Đánh giá off-policy (OPE)

- [ ] `shared/off_policy_evaluation.py`: IPS, SNIPS, Doubly Robust + bootstrap CI; `scripts/ope_report.py`.
- [ ] Test trên dữ liệu tổng hợp có đáp án biết trước.

### WP6.3 Chính sách "tự làm hay gọi người" (execute vs escalate)

- [ ] Contextual bandit (Vowpal Wabbit hoặc River, đã có `scripts/benchmark_vw_bandit.py`) chạy **shadow**: ghi quyết định đề xuất, không hành động.
- [ ] Ràng buộc rủi ro 3 chiều (theo hướng risk-constrained remediation): blast radius (số OSD/PG ảnh hưởng), reversibility (có rollback plan đã diễn tập), uncertainty (bất đồng ensemble/độ tin chẩn đoán) — vượt một chiều là escalate.
- [ ] Ngân sách FRR do operator đặt (mặc định 5%/tháng/cluster); vượt → tự hạ về APPROVAL_REQUIRED + cảnh báo.
- [ ] Tích hợp: đầu ra bandit chỉ là **một input** cho `autopilot_guardrails` + Trust Engine; không bỏ qua kill switch/cooldown/budget hiện có.

### WP6.4 Lộ trình nâng quyền

- [ ] Shadow ≥ 30 ngày, OPE DR cho thấy FRR ≤ ngân sách với CI 95% → đề xuất canary cho **1 action × 1 cluster**, có operator approval ghi audit (như promotion model registry).
- [ ] Canary 14 ngày, theo dõi FRR thật, regressed_24h, số escalation; tự rollback về APPROVAL_REQUIRED khi vi phạm.
- [ ] Test: guardrail vẫn chặn khi kill switch bật dù bandit chọn execute; vi phạm FRR tự hạ quyền.

**Nghiệm thu WP6:** ≥ 1 action chạy LIMITED_AUTOPILOT trên 1 cluster 14 ngày với FRR ≤ ngân sách, không incident do automation.
**Ước lượng:** 8–10 ngày + thời gian shadow/canary.

---

## WP7 — Online learning forecasting (tiếp nối, ưu tiên P1)

- [ ] Sau deploy `0670de30`: xác nhận `online_learner_cycle_audit` không còn `update_failed`, có `READY_TO_LEARN` và `update_applied=true` (log đã có traceback nếu lỗi).
- [ ] Chạy `scripts/river_v2_promotion_evidence.py` hằng tuần, lưu JSON; theo dõi verified/scored/self-labelled.
- [ ] Khi ≥ 20 verified outcome/scope ở ≥ 3 scope: mở rộng canary online learning từ 1 host/cpu sang 1 cluster (cpu+memory), vẫn SHADOW_ONLY.
- [ ] Theo dõi `paired_holdout` (`scripts/river_linear_v2_runtime_replay.py`): chỉ đề xuất promotion khi verdict `CANDIDATE_BETTER` ổn định ≥ 2 tuần.
- [ ] Gắn sự kiện forecast vào WP6 như feature (dự báo đầy disk/CPU).

**Ước lượng:** 1 ngày công + theo dõi định kỳ.

---

## WP8 — Quan sát, quản trị và tài liệu

- [ ] Trang `/ai-learning`: thẻ KPI (WP0), chất lượng LF (WP2.3), evidence coverage (WP3), OPE/FRR (WP6).
- [ ] Báo cáo tuần tự động (Telegram + JSON trong `docs/benchmark/`): KPI, top incident, verdict mới, playbook gần đạt Trust Engine.
- [ ] `docs/ai-evaluation.md`, `docs/production-release-runbook.md`: bổ sung quy trình verdict, chaos, nâng quyền.
- [ ] Mỗi WP thêm browser acceptance check nếu có UI; static-analysis budget/coverage gate phải pass; critical-path coverage không giảm.

---

## 9. Thứ tự, phụ thuộc và lịch dự kiến

| Tuần | Work package | Phụ thuộc | Mốc |
|---|---|---|---|
| 1 | WP0, WP1.1, WP2.1 | — | Baseline + verdict Telegram + NODE_UNREACHABLE hysteresis |
| 2 | WP1.2, WP1.3, WP2.2 | WP0 | Incident ÷5, ≥ 30 verdict/tuần |
| 3 | WP2.3, WP2.4, WP4 | WP2.1 | Nhãn tự động, Disk Risk có dự đoán |
| 4–5 | WP3.1 → WP3.4 | WP1 (bớt nhiễu), WP4 | `investigate_manually` ≤ 40% |
| 5 | WP7 (sau deploy) | deploy thủ công | Online learning có mẫu học |
| 6–7 | WP5 | staging cluster | ≥ 50 nhãn ground truth |
| 8–10 | WP6.1 → WP6.3 (shadow) | ≥ 200 nhãn | OPE report |
| 11–13 | WP6.4 canary | shadow ≥ 30 ngày | 1 action LIMITED_AUTOPILOT |

## 10. Rủi ro và cách giảm

| Rủi ro | Ảnh hưởng | Giảm thiểu |
|---|---|---|
| Hysteresis che mất sự cố thật | Phát hiện muộn | Replay 30 ngày trước khi bật; giữ cảnh báo "host chập chờn"; ngưỡng cấu hình được |
| Operator không bấm verdict | Không có nhãn | Nút một chạm + lý do chọn sẵn + nhắc 10 case/ngày + KPI verdict/tuần |
| Nhãn tự động sai | Học sai | LF không cấp quyền; đo precision so với nhãn operator; trọng số theo precision |
| Collector làm nặng cluster | Ảnh hưởng hiệu năng | Giới hạn đồng thời/timeout/circuit breaker; chỉ read-only; đo SSH call count |
| Collector vô tình ghi | Mutation ngoài ý muốn | Allowlist typed + chặn mutation verb ở transport + test |
| Chaos lọt sang production | Sự cố thật | Allowlist cluster staging + token + hoàn tác bắt buộc + test guard |
| Bandit học từ dữ liệu lệch | Quyết định sai | OPE DR + CI; shadow ≥ 30 ngày; ngân sách FRR; auto hạ quyền |
| Thay đổi ngưỡng Ceph sai | Mất cảnh báo | Action RISKY cần duyệt, preflight + rollback giá trị cũ |

## 11. Định nghĩa hoàn thành (Definition of Done) cho mỗi hạng mục

- Code + migration (nếu có) + test đơn vị/tích hợp pass cục bộ và trong CI.
- Static-analysis budget pass; coverage không giảm; critical path không giảm.
- Tài liệu vận hành cập nhật; Settings mới có mô tả.
- Đo KPI trước/sau bằng WP0 và ghi vào §12 với commit + số liệu.
- Với thay đổi hành vi production: có rollback bằng cấu hình và đã thử rollback.

## 12. Nhật ký thực hiện

| Ngày | Hạng mục | Kết quả | Evidence | Trạng thái |
|---|---|---|---|---|
| 26/09/2026 | Lập plan | Baseline §1 đo trên DB production (read-only) | Bảng §1 | Recorded |
| 28/09/2026 | WP0 KPI | Script + API + thẻ KPI; baseline 6.817 incident, reopen 38,3%, placeholder 97,5%, 0 verdict | `docs/benchmark/autonomy-kpi-baseline-2026-09-28.json` | Accepted |
| 28/09/2026 | WP1.1 NODE_UNREACHABLE | Hysteresis 3 lần OK + giữ mở khi flapping (≥3 incident/giờ, ổn định 1 giờ mới đóng), 1 cảnh báo chập chờn; replay 1.992 → 231 (−88,4%) | `tests/test_node_health_monitor.py` 22 passed | Partial (chờ deploy đo thật) |

## 13. Quy tắc trạng thái

- `[ ]` Chưa có implementation hoặc evidence đủ dùng.
- `[~]` Có implementation/test một phần nhưng chưa đạt nghiệm thu.
- `[x]` Chỉ dùng khi có commit, test, số liệu KPI trước/sau và rollback note.
- `[!]` Bị chặn bởi hạ tầng, dữ liệu (thiếu nhãn) hoặc quyết định operator.

## 14. Tài liệu tham khảo

- RCA bằng LLM agent có công cụ thu thập chẩn đoán: https://arxiv.org/html/2403.04123v1
- RCACopilot (retrieval sự cố cũ + thu thập chẩn đoán): https://yinfangchen.github.io/assets/pdf/llm_rca.pdf
- HolmesGPT (agent chỉ-đọc theo runbook, CNCF sandbox): https://github.com/HolmesGPT/holmesgpt
- Keep (dedup fingerprint, correlation): https://github.com/keephq/keep
- Ceph diskprediction/devicehealth: https://docs.ceph.com/en/latest/mgr/diskprediction/
- Ceph health checks (BLUESTORE_SLOW_OP_ALERT): https://docs.ceph.com/en/latest/rados/operations/health-checks/
- Risk-constrained remediation (FRR budget, 3 chiều rủi ro): https://arxiv.org/html/2607.20005
- Vowpal Wabbit contextual bandits: https://vowpalwabbit.org/docs/vowpal_wabbit/python/9.6.0/tutorials/python_Contextual_bandits_and_Vowpal_Wabbit.html
- Doubly Robust off-policy evaluation: https://icml.cc/2011/papers/554_icmlpaper.pdf
- Snorkel weak supervision: https://arxiv.org/pdf/1711.10160
