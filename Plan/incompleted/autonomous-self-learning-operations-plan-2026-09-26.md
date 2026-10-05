# Autonomous Self-Learning Operations Plan — Ceph AI

**Ngày lập:** 26/09/2026
**Trạng thái:** `IN-PROGRESS` — code WP0–WP4, WP6.1–6.3, báo cáo tuần và WP1.2 đã merge (xem bảng log cuối file); đã deploy immutable lên CS-LAB từ 29/09, nhưng **incident từ health check của cụm mặc định không được tạo từ 15/09 tới 02/10** (thiếu service `remediation-watcher` trong Compose, sửa ở `fbceb4dc`), nên KPI sau deploy phải đo lại từ 02/10. Tính năng thu evidence mặc định TẮT (`INVESTIGATION_ENABLED=false`), bật theo cluster canary bằng `INVESTIGATION_CLUSTER_IDS`.
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

### WP1.2 (điều chỉnh theo dữ liệu 28/09) — `BLUESTORE_SLOW_OP_ALERT` và `OSD_LATENCY_HIGH`

**Phát hiện 28/09:** 1.025/1.040 incident BlueStore xảy ra **trong một ngày (06/09)**, chồng lên nhau (tạo trùng khi incident trước còn mở) — một đợt bão lịch sử, nay đã bị chặn bởi unique index in-flight (`uq_incidents_inflight_cluster_code`); sau đó ~1 incident/ngày. Nhiễu **hiện tại** (7 ngày: 1.833 incident) là `NODE_UNREACHABLE` 1.157 (WP1.1), `CRUSH_SKEW_PG/USE` 369 (WP1.4 mới), `OSD_LATENCY_HIGH` 256.

- [x] `OSD_LATENCY_HIGH`: incident chỉ sống ~2 phút (mở sau 2 scan, đóng sau 1 scan tốt). Thêm `osd_latency_open_scans=4`, `osd_latency_recovery_scans=3`; replay 7 ngày **256 → 48 (−81%)**; test `tests/test_osd_latency_monitor.py` (16 passed).
- [x] Phần BlueStore dưới đây đã làm ngày 02/10 theo yêu cầu operator (trước đó hoãn vì không còn là nguồn nhiễu).

#### BlueStore: từ incident lặp sang tín hiệu xu hướng

**Hiện trạng:** 1.040 incident/30 ngày; router có nhánh self-heal restart OSD có điều kiện (`router_client.py` ~1204).

- [x] Mẫu theo cluster trong bảng mới `bluestore_slow_op_samples` (migration `m20261002bluestoreslowops`, giữ 30 ngày): OSD bị ảnh hưởng + host đã tra thật, tối đa 1 mẫu/5 phút trừ khi tập OSD đổi (`4c788fe3`). **Điều chỉnh:** Ceph bản này không ghi số slow op trong `health detail` (chỉ "osd.N observed slow operation indications"), nên xu hướng tính theo **số OSD bị ảnh hưởng**; số op cụ thể lấy qua runbook WP3.
- [x] Quy tắc mở (`watcher/bluestore_slow_ops.py`, gắn vào cả vòng cụm mặc định lẫn cụm observed): ≥ 2 OSD cùng host, **hoặc** số OSD > P95 nền 14 ngày × 1,5 (≥ 2 OSD, cần ≥ 12 mẫu nền), **hoặc** đợt kéo dài > 25 giờ. **Lý do T = 25 giờ:** CS-LAB dùng `bluestore_slow_ops_warn_lifetime=86400`, `threshold=1` — một slow op đơn lẻ giữ cảnh báo 24 giờ, nên chỉ đợt vượt một vòng lifetime mới là slow op tái diễn. Không đạt → chỉ lưu mẫu. Đợt được khôi phục từ mẫu sau restart; lỗi bất kỳ của bộ lọc → mở incident như cũ (fail-open).
- [x] Action `tune_bluestore_slow_ops_warn` (`49b6cba3`): management action RISKY qua Chat, tham số đóng (`device_class` hdd/ssd/nvme, `threshold` 1–1000, `lifetime_seconds` 60–86400), `ceph config set osd/class:<class> …`. Preflight in giá trị hiện tại vào output thực thi và **từ chối nếu class đã có override**, nên rollback `ceph config rm` đúng 2 khoá luôn trả về trạng thái cũ (`tune_bluestore_slow_ops_warn_rollback_command`). Không đăng ký vào playbook incident: LLM chẩn đoán không có nguồn tham số xác định; operator đề xuất qua Chat.
- [x] Liên kết WP3: evidence của incident có `osd_id`/`host`, nên runbook BlueStore nay thật sự thu `dump_historic_slow_ops` (trước đó luôn bị bỏ qua vì thiếu `osd_id`), `ceph osd metadata` và tải host. SMART: như WP4, đĩa virtio của CS-LAB không có SMART thật — chưa thêm.
- [x] Test: `tests/test_bluestore_slow_ops.py` (14), `tests/test_tune_bluestore_slow_ops_warn.py` (14), `tests/test_bluestore_slow_op_replay.py` (3).

**Nghiệm thu:** incident BlueStore giảm ≥ 70% mà không bỏ sót trường hợp slow op tăng đột biến (test replay với 3 đợt thật trong dữ liệu).
**Kết quả replay (02/10, `scripts/bluestore_slow_op_replay.py` → `docs/benchmark/bluestore-slow-op-replay-2026-10-02.json`):** 1.040 → 8 incident (−99,2%), cả 3 đợt (06/09, 07/09, 14/09) vẫn được báo. **Lưu ý trung thực:** mức giảm này gần như toàn bộ đến từ "một incident mỗi đợt" — unique index in-flight hiện có cũng cho đúng 8; mọi đợt lịch sử đều có 2 OSD cùng host nên `same_host` luôn mở. Bộ lọc chỉ bớt thêm slow op đơn lẻ 1 OSD (giả lập 1 OSD/đợt: 3 incident, cố ý không báo 2 đợt ngắn 07/09 và 14/09). Cần đo lại sau deploy.
**Ước lượng:** 2 ngày.

### WP1.4 (mới, 28/09) — Gom `CRUSH_SKEW_PG/USE` theo cluster

**Dữ liệu 7 ngày:** 274 `CRUSH_SKEW_PG` + 95 `CRUSH_SKEW_USE`, tách thành 12 mã (theo OSD và host~class), sống 5–30 phút, cách nhau hàng giờ — là sự kiện thật (rebalance) bị chẻ nhỏ. Hysteresis chỉ giảm ≤ 45% và làm trễ 25 phút → không phù hợp.

- [x] Một incident `CRUSH_SKEW_PG` / `CRUSH_SKEW_USE` mỗi tín hiệu (không hậu tố entity); entity lệch nằm trong evidence + `action_params`, sắp theo |skew|; incident cũ theo entity được đóng khi chuyển sang dạng gom.
- [x] Cập nhật evidence khi tập entity đổi (không cảnh báo lại); đóng khi không entity nào còn lệch (giữ `still_over_threshold` qua restart). `watcher/main.py` + `ceph_code_families.py` nhận mã gom.
- [x] Replay 7 ngày: PG 274 → 47, USE 95 → 19 (**−82%**); test `tests/test_crush_skew_monitor.py` (29 passed).

### WP1.3 `OSD_LATENCY_HIGH:N` và health check tái mở

- [x] Hysteresis cho `OSD_LATENCY_HIGH:*` đã làm trong WP1.2 (mở 4 scan, đóng 3 scan).
- [x] Đánh giá lại 03/10: `NODE_UNREACHABLE` sau WP1.1 chỉ còn 3–26 incident/ngày (853/980 lần mở lại trong 7 ngày đều rơi vào 26–29/09, trước deploy). Nhưng khi incident health check chạy lại (02/10), health check chung mở lại 55% trong 30 phút trong đêm ceph1 chập chờn (`MON_DOWN` 32/14, `SLOW_OPS` 32/11, `OSD_DOWN`, `PG_DEGRADED`…) — con số 5,7% trước đây đo khi incident chung gần như không được tạo.
- [x] Reopen suppression chung bằng **trì hoãn đóng** thay vì mở lại incident cũ (`watcher/resolve_grace.py`, `b8250a2d`): incident chung chỉ RESOLVED khi check vắng mặt liên tục `incident_resolve_grace_seconds` (mặc định 1800; 0 = như cũ); check quay lại trong grace → giữ incident đang mở, không alert/chẩn đoán mới, ghi audit `incident_recurred`. Mô phỏng grace trên dữ liệu 26 giờ: 0/10/30 phút → mở lại 74/45/19 (−74% ở 30 phút), incident 135/99/61. Đồng hồ theo (cluster, mã) trong RAM; restart chỉ làm đóng chậm tối đa 1 grace. Đánh đổi: thông báo "đã khắc phục" và việc huỷ action chờ duyệt chậm tối đa 30 phút.
- [x] Test (`tests/test_resolve_grace.py`, 6): trong/ngoài cửa sổ, quay lại rồi đếm lại từ đầu, khác cluster/mã không gộp, grace 0, audit `incident_recurred` (thay cho tên `REOPENED` trong plan cũ vì incident không bị mở lại).

**Nghiệm thu:** tỉ lệ tái mở (WP0) giảm ≥ 60%. **Kết quả:** replay −74% số lần mở lại (tỉ lệ 54,8% → 31,1%); cần đo KPI thật 7 ngày sau deploy.
**Ước lượng:** 1,5 ngày.

---

## WP2 — Thu nhãn (ưu tiên P0)

### WP2.1 Verdict một chạm trên Telegram

**Hiện trạng:** endpoint `POST /incidents/{id}/cases/{case_id}/verdict` (`dashboard/routes/incidents.py:592`) yêu cầu Dashboard; verdict xấu cần ghi chú ≥ 5 ký tự → không ai dùng.

- [x] Callback `v:<action ref>:<code>` trong `dashboard/telegram_approval_bot.py` (≤ 64 byte, có test); mã C/X/U/I + lý do F1/F2/F3/E1.
- [x] Sau mỗi Duyệt/Từ chối, tin nhắn hỏi "Chẩn đoán của AI có đúng không?" (khi case chưa có verdict):
  - hàng 1: `✅ Đúng` `❌ Sai` `⚠️ Nguy hiểm` `🤷 Chưa rõ`,
  - khi bấm `❌`/`⚠️`: sửa tin nhắn hiển thị **lý do chọn sẵn** (đáp ứng yêu cầu ghi chú): "Cảnh báo sai", "Chẩn đoán sai nguyên nhân", "Hành động không cần thiết", "Hành động có thể gây hại", "Không khắc phục được" → map sang `FALSE_POSITIVE`/`INEFFECTIVE`/`UNSAFE` + `operator_note` = lý do.
- [x] Tái dùng chat trust theo cluster + `TELEGRAM_APPROVAL_USER_IDS`; `operator_verdict_by = telegram:<username|id>`.
- [x] Ghi đè có audit (verdict mới/cũ, note, actor đầy đủ trong evidence; actor audit cắt 32 ký tự vì cột VARCHAR(32)).
- [x] `shared/remediation_cases.record_verdict` dùng chung Dashboard + Telegram (validation giống nhau).
- [x] Test: nút + lý do, ≤ 64 byte, chat/cluster không tin cậy và người ngoài allowlist bị chặn, mã lạ bị bỏ qua, audit, actor dài (8 test mới).

**Nghiệm thu:** ≥ 30 verdict/tuần trong 2 tuần đầu (đo WP0).
**Ước lượng:** 1,5 ngày.

### WP2.2 Nhắc đánh giá có chủ đích

- [x] `shared/verdict_nudges.select_cases`: tối đa `verdict_nudge_limit` (mặc định 5) case chưa verdict trong 14 ngày; ưu tiên outcome rõ, AI tự tin nhưng bị từ chối, family ít nhãn, đề xuất cụ thể; ≤ 2 case/family; không nhắc lại case đã nhắc (event timeline, không cần migration).
- [x] Chạy trong vòng `_notify_loop` của service Telegram, sau `verdict_nudge_hour` (mặc định 9h VN), mỗi case 1 tin kèm nút verdict qua đúng kênh cluster; một lần/ngày, kiểm tra trong DB nên restart không gửi trùng.
- [x] `tests/test_verdict_nudges.py` (4) + 2 test gửi trong `tests/test_telegram_approval_bot.py`.

**Ước lượng:** 1 ngày.

### WP2.3 Nhãn tự động kiểu weak supervision (không cấp quyền)

- [x] Module `shared/auto_labels.py` với các labeling function (LF) thuần, trả `CORRECT | FALSE_POSITIVE | INEFFECTIVE` hoặc bỏ phiếu trắng (abstain), kèm trọng số:
  - `lf_self_resolved_without_action` (FALSE_POSITIVE, 0.6): incident RESOLVED trong ≤ 30 phút, không có action được thực thi,
  - `lf_postcheck_passed` (CORRECT, 1.0): outcome `VERIFIED_SUCCESS` và `regressed_24h=false`,
  - `lf_regressed` (INEFFECTIVE, 1.0): `regressed_1h`/`regressed_24h=true`,
  - `lf_execution_failed` (INEFFECTIVE, 0.5),
  - `lf_flapping` (FALSE_POSITIVE, 0.7): incident có cờ `flapping` của WP1.1 và không thực thi action,
  - `lf_reopened_after_execution` (INEFFECTIVE, 0.8): cùng code mở lại trong 24 giờ sau khi thực thi.
  - `lf_rejected_then_self_resolved` gộp vào `lf_self_resolved_without_action` (action REJECTED thì không được thực thi).
- [x] ~~Bảng `remediation_case_auto_labels` + migration~~ — **đổi hướng:** nhãn được tính khi cần từ các dòng đã có (2 giây cho 30 ngày), nên không thêm bảng/migration; tổng hợp bằng biểu quyết có trọng số. Khi có ≥ 20 nhãn operator chồng lấp sẽ đặt lại trọng số theo precision đo được.
- [x] **Tuyệt đối không** ghi vào `operator_verdict`, không dùng trong `trust_engine` để mở autopilot; có test chứng minh.
- [x] Báo cáo chất lượng LF: `scripts/auto_label_report.py` (chỉ đọc) — coverage, conflict, precision so với nhãn operator.
- [x] Test từng LF + tổng hợp + `collect_facts` + không đụng `operator_verdict` (`tests/test_auto_labels.py`).
- [x] Dùng weak label trong `ai_evaluation`: đo coverage/precision/recall/F1 và ma trận nhầm lẫn; chỉ chấm trên operator verdict độc lập. `SELF_RESOLVED`/`FLAPPING` giữ là weak class riêng và chỉ dùng `FALSE_POSITIVE` làm proxy khi đối chiếu.
- [x] Dùng weak label/xung đột LF để xếp hạng và cân bằng batch nhắc verdict (WP2.2); tín hiệu chỉ nằm trong audit event, không làm lộ dự đoán cho operator trước khi đánh giá.
- [x] Hiển thị coverage, so sánh độc lập, xung đột LF, metrics theo nhãn/LF trên `/ai-learning`; lọc facts theo cluster và giữ legacy unscoped rows chỉ trong cluster mặc định.
- [x] Giữ nguyên ranh giới an toàn: không ghi `operator_verdict`, không thay đổi trust/autonomy.

**Kết quả 28/09/2026 (production, 30 ngày, `docs/benchmark/auto-labels-2026-09-28.json`):** 3.835/4.157 case (92%) có nhãn tự động — số liệu này được tạo trước khi `SELF_RESOLVED`/`FLAPPING` tách khỏi verdict-like classes, nên không dùng làm precision hiện tại. Metrics mới chỉ có ý nghĩa khi tồn tại operator verdict độc lập chồng lấp; không suy diễn weak class thành operator truth.

**Nghiệm thu:** ≥ 60% case trong 30 ngày có ít nhất 1 nhãn tự động; precision LF được báo cáo.
**Ước lượng:** 2 ngày.

### WP2.4 Trang đánh giá nhanh trên Dashboard

- [x] Trong Alert Center (`/alerts`), thêm cột verdict (case mới nhất của nhóm) và thao tác nhanh (một chạm "✓ Đúng", hoặc chọn verdict khác kèm ghi chú), dùng chung endpoint + `record_verdict` nên vẫn chỉ admin, vẫn bắt ghi chú cho verdict âm, vẫn audit; lọc "chưa có nhãn / đã có nhãn"; sau khi lưu quay lại đúng trang/bộ lọc (`next` chỉ nhận `/alerts…`, chống open redirect).
- [x] Browser acceptance (`scripts/browser_acceptance.mjs`) thêm check `alert_verdicts` (chỉ đọc, không submit form).

**Kết quả 28/09/2026:** production có 195 nhóm cảnh báo, 119 nhóm có case chưa nhãn; truy vấn case thêm 0,1 s. Test: `tests/test_alert_center_verdicts.py` (5 passed).

**Ước lượng:** 1 ngày.

---

## WP3 — Điều tra tự động chỉ-đọc (evidence trước chẩn đoán) (ưu tiên P1)

**Mục tiêu:** thay `investigate_manually` bằng bộ bằng chứng thu tự động, không cần duyệt vì chỉ đọc.

### WP3.1 Registry evidence collector

- [x] `shared/evidence_collectors.py`: 15 collector, mỗi cái có id, mô tả, loại (CEPH qua MON / HOST qua SSH read-only), lệnh cố định + tham số kiểm tra bằng regex (`osd_id`, `devid`, `target`), timeout, giới hạn kích thước, redaction (`shared/ai_redaction.redact_text`):
  - Ceph: `health detail`, `osd tree`, `osd perf`, `osd df`, `pg dump_stuck`, `device ls`, `crash ls-new`, `time-sync-status`, `osd metadata N`, `tell osd.N dump_historic_slow_ops`, `device get-health-metrics <devid>`,
  - Host: `uptime`, `free -m`, `ip -s link`, `ping -c 3 -W 1 <target>` từ MON.
  - **Chưa có** (cần quyền root, khóa read-only không có): `smartctl`, `dmesg`, `journalctl`, `iostat` — để lại cho khi có identity chẩn đoán riêng.
- [x] Kiểm soát read-only: mọi lệnh sau khi render đều qua `assert_read_only` (động từ ghi giống `scripts/live_readonly_acceptance.py` + ký tự shell), kiểm tra lại lần nữa ở transport `SshTransport` trước khi gửi.
- [x] Giới hạn: ≤ 2 collector đồng thời/host, ≤ 1 lượt/incident/10 phút (`claim`), ≤ 60 s/lượt (phần còn lại `skipped_budget`), circuit breaker theo host (3 lỗi → nghỉ 5 phút, half-open).
- [x] Test: allowlist, lệnh ghi/chuỗi lệnh bị chặn, tham số độc hại, redaction, cắt output, ngân sách, breaker, concurrency, cooldown, transport chặn trước khi gửi (`tests/test_evidence_collectors.py`, 28 passed).

**Kết quả 28/09/2026 (CS-LAB, chỉ đọc):** 11/11 collector thử đều `ok`, tổng ~29 s; lệnh `ceph` 3–5 s/lệnh (cephadm shell), lệnh host ~0,2 s. Vì ngân sách 60 s, runbook WP3.2 nên giới hạn ~8 lệnh `ceph`/incident.

### WP3.2 Runbook điều tra dạng dữ liệu

- [x] `worker/policy/investigation_runbooks.yaml`: theo fault family → danh sách collector (+ chỗ chạy `mon`/`incident_host`, tham số từ ngữ cảnh `{host}`/`{osd_id}`/`{devid}`) + câu hỏi chẩn đoán + tiêu chí kết luận (`when`/`then`, đầu vào cho WP3.4); có runbook `default` cho mã lỗi mới.
- [x] Runbook cho top code: `NODE_UNREACHABLE`, `BLUESTORE_SLOW_OP_ALERT`, `OSD_LATENCY_HIGH`, `OSD_DOWN`, `MON_CLOCK_SKEW`, `PG_DEGRADED`, `POOL_NEARFULL`, `LARGE_OMAP_OBJECTS`, `DEVICE_HEALTH`, `SLOW_OPS`, thêm `CRUSH_SKEW_USE`/`CRUSH_SKEW_PG` (WP1.4).
- [x] Validator + test (`shared/investigation_runbooks.py`, `tests/test_investigation_runbooks.py`, 22 passed): collector phải có trong registry, HOST cần `host`, CEPH không được có `host`, tham số khớp đúng collector, chỉ dùng ngữ cảnh cho phép, ≤ 8 lệnh ceph/runbook; ngữ cảnh lấy từ hậu tố mã lỗi và được kiểm tra regex; thiếu ngữ cảnh thì **bỏ qua có ghi lý do**, không đoán.

**Kết quả 28/09/2026 (CS-LAB, chỉ đọc):** runbook `OSD_LATENCY_HIGH:1` cho incident thật: 5/5 collector ok, 37 s (`osd df` 14 s). Ngân sách 60 s đủ nhưng sát — WP3.3 nên chạy nền, không chặn luồng tạo incident.

### WP3.3 Chạy tự động khi incident mở

- [x] Chạy tự động: **đổi hướng** — thay vì móc vào >10 chỗ tạo incident, `watcher/investigation_scanner.py` là một scan phụ của Watcher (thread riêng, không chặn phát hiện): mỗi 60 s lấy ≤ 2 incident đang mở, ≤ 30 phút tuổi, chưa có evidence → chạy runbook → lưu `incident_evidence(incident_id, runbook, collector_id, target, status, command, output_redacted, truncated, duration_ms, created_at)` + migration `m20260928incidentevidence` + sự kiện timeline `evidence_collected`. Incident lặp của host FLAPPING được đánh dấu `skipped_flapping`, không SSH; một fault family/cluster chỉ chạy 1 lần/10 phút (cơn bão cùng loại chỉ tốn 1 lượt). Cấu hình: `INVESTIGATION_ENABLED`, `INVESTIGATION_SCAN_INTERVAL_SECONDS`, `INVESTIGATION_MAX_AGE_MINUTES`, `INVESTIGATION_INCIDENTS_PER_SCAN`.
- [x] Hiển thị evidence trên timeline incident (tóm tắt 3 dòng + từng collector, output đã redact).
- [x] Tóm tắt trên Telegram: `worker/llm/evidence_gate.annotate` ghép `summary_lines` vào `diagnosis_text`, nên tin cảnh báo AI mang theo tóm tắt bằng chứng.
- [x] Cluster quan sát: worker thu evidence cho incident của mọi cluster ngay trước khi chẩn đoán (WP3.4), scanner chỉ còn là lưới an toàn cho cluster mặc định.
- [x] Test end-to-end với transport giả (`tests/test_investigation_scanner.py`, 8 passed) + trang timeline.

### WP3.4 Chẩn đoán xác định trước LLM

- [x] `shared/deterministic_triage.py`: luật đọc evidence WP3.3, mỗi kết luận có trích dẫn evidence và độ tin; thiếu/mơ hồ → `UNKNOWN`, không đoán; không bao giờ tự đề xuất reboot:
  - `NODE_UNREACHABLE`: SSH ok → `TRANSIENT` hoặc `HOST_RECENTLY_REBOOTED` (uptime ≤ 15 phút); ping ok + SSH lỗi → `SSH_UNAVAILABLE` (kiểm tra sshd/firewall); ping lỗi + có OSD down → `HOST_DOWN` (IPMI/nguồn); ping lỗi + mọi OSD up → `MGMT_NETWORK` (**không** reboot).
  - `OSD_LATENCY_HIGH`: ≥ 50% OSD ≥ 100 ms → `CLUSTER_WIDE_LOAD`; OSD đích ≥ 3× trung vị → `SINGLE_OSD_OUTLIER` (xem SMART trước khi restart); còn lại → `RECOVERED`.
  - `MON_CLOCK_SKEW`: nêu MON lệch + action `resync_ntp` (cần duyệt; test kiểm tra action có trong `action_policy.yaml`).
- [x] Scanner WP3.3 ghi kết luận vào timeline (`triage_concluded`, cả `UNKNOWN` để đo độ phủ); trang timeline hiển thị kết luận + khuyến nghị.
- [x] Cổng `worker/llm/evidence_gate.py` trong `diagnose_incident`: nếu incident chưa có evidence, worker tự thu (cùng runbook/collector/giới hạn, lưu vào `incident_evidence` nên scanner bỏ qua) → chạy luật; kết luận đã biết với độ tin ≥ `TRIAGE_MIN_CONFIDENCE` (0.75, và ≥ `ai_min_diagnosis_confidence`) **thay thế lời gọi LLM**; action (nếu có) vẫn qua đúng phân loại/preflight/duyệt như đề xuất LLM (vd. `resync_ntp` là SAFE theo policy hiện hành), kết luận không action → `investigate_manually`. Lỗi thu evidence không bao giờ chặn chẩn đoán. Verified case vẫn đi qua `find_verified_cases` như trước.
- [x] Khi luật `UNKNOWN`/kém tin: prompt LLM thêm evidence thật dạng `[E#]` (≤ 4000 ký tự) + yêu cầu trích `[E#]` cho mỗi khẳng định, thiếu bằng chứng thì nói chưa chắc chắn. **Chưa đo** tỉ lệ trích dẫn/hallucination bằng `ai_evaluation` (cần dữ liệu sau deploy).
- [x] Test luật (`tests/test_deterministic_triage.py`, 24 passed) + scanner/timeline. Chạy trên evidence thật CS-LAB: `NODE_UNREACHABLE`→`TRANSIENT`, `OSD_LATENCY_HIGH:1`→`RECOVERED`, `MON_CLOCK_SKEW`→`RECOVERED`, khớp trạng thái thực.

**Nghiệm thu WP3:** ≥ 90% incident top-10 code có evidence trong ≤ 2 phút; `investigate_manually` ≤ 40%; không có lệnh ghi nào trong audit của collector.
**Ước lượng:** 5–7 ngày.

---

## WP4 — Tận dụng tính năng sẵn có của Ceph (ưu tiên P1)

- [x] Phát hiện trạng thái module mgr (`ceph mgr module ls`): `devicehealth`, `diskprediction_local`, `pg_autoscaler`, `balancer` — read-only (`shared/ceph_features.py`, tập lệnh cố định). **Đổi chỗ hiển thị:** card trên trang Disk Risk (`/api/ceph-features`, cache 10 phút/cluster) thay vì Capability Matrix, vì Capability Matrix là bảng tài liệu do admin nhập tay.
- [ ] ~~Action approval-required `enable_mgr_module`~~ — **hoãn có bằng chứng:** CS-LAB dùng đĩa ảo virtio; `ceph device get-health-metrics` trên 3 device lấy mẫu: 1 rỗng, 2 chỉ có bản ghi `smartctl failed` (sudo exit 137) — module phân biệt bản ghi lỗi này với SMART thật. Vì vậy bật `diskprediction_local` không sinh dự đoán nào. Card hiển thị khuyến nghị bật (kèm lệnh, cần duyệt) chỉ khi có dữ liệu SMART. Làm action khi có cluster đĩa vật lý.
- [x] Đọc `ceph device ls` (life expectancy) + lấy mẫu `get-health-metrics` tối đa 3 device (ưu tiên OSD, devid được kiểm tra regex trước khi đưa vào lệnh) để biết SMART có khả dụng không. Ingest định kỳ làm feature cho WP3/WP6: chưa làm.
- [x] `pg_autoscaler`/`balancer`: chỉ đọc trạng thái + khuyến nghị (`ceph osd pool autoscale-status`, `ceph balancer status`), không tự bật.
- [x] Test parser với fixture lấy từ CS-LAB đã redact, gồm định dạng `always_on_modules` kiểu cũ (`tests/test_ceph_features.py`).

**Kết quả 28/09/2026 (CS-LAB, chỉ đọc):** devicehealth/balancer/pg_autoscaler luôn bật; diskprediction_local tắt; 9 device, 0 có dự đoán, 0/3 mẫu có SMART (2 smartctl lỗi); balancer upmap, phân bố đã tối ưu; autoscaler không đề xuất thay đổi. Tiêu chí "≥ 80% device có life expectancy" không thể đạt trên đĩa ảo.

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

- [x] Bảng `autonomy_decisions(id, case_id, incident_id, cluster_id, fault_family, context_json, candidates_json, chosen_action, chosen_by{rules,llm,deterministic}, propensity, policy_version, created_at)` + migration `m20260928autonomydecisions`. **Đổi hướng:** không lưu `outcome/reward` — `shared/decision_log.reward_for` tính lúc báo cáo từ verdict operator (ưu tiên) rồi outcome đã verify, để reward không bao giờ cũ. Bắt đầu ghi **trước** điều kiện ≥ 200 nhãn để dữ liệu tích luỹ sẵn; phần học (WP6.3) vẫn chờ đủ nhãn.
- [x] Worker ghi mọi đề xuất mới trong cùng transaction tạo Action/RemediationCase (kể cả đề xuất sau này bị từ chối — verdict/outcome gắn qua case), nguồn quyết định `rules`/`llm`/`deterministic`, propensity 1.0 (chưa có exploration).
- [x] Feature có cấu trúc, không văn bản tự do/IP: fault family, severity, trạng thái cluster, số node ảnh hưởng, giờ UTC, độ tin chẩn đoán, trust score + số mẫu shadow, classification, kết luận + độ tin triage (WP3.4). **Chưa có:** degraded %, recovery, device class.

### WP6.2 Đánh giá off-policy (OPE)

- [x] `shared/off_policy_evaluation.py`: IPS, SNIPS, Doubly Robust, bootstrap CI (percentile), effective sample size và **support** (tỉ lệ log mà policy đích đặt xác suất > 0 lên action đã log); `scripts/ope_report.py` (chỉ đọc) báo cáo và cảnh báo khi toàn bộ propensity = 1.0 (chỉ đánh giá được chính policy đang chạy).
- [x] Test trên bandit tổng hợp có giá trị thật tính tay (0,85): IPS/SNIPS nằm trong CI, DR chính xác với reward model đúng và tự sửa khi reward model sai; log tất định → support 0, không đưa ra con số (`tests/test_off_policy_evaluation.py`, `tests/test_decision_log.py`).

### WP6.3 Chính sách "tự làm hay gọi người" (execute vs escalate)

- [x] Chính sách shadow `shared/shadow_policy.py` (luật có ràng buộc rủi ro, **chưa** phải bandit học — chưa đủ nhãn): mỗi quyết định WP6.1 được gắn `shadow_recommendation` = execute/escalate + lý do (migration `m20260928shadowpolicy`), không hành động. Bandit học (VW/River) thay cho luật này khi có ≥ 200 nhãn.
- [x] Ràng buộc rủi ro: contract đăng ký, blast radius (node ảnh hưởng ≤ `max_targets` của contract), reversibility (contract có rollback), uncertainty (độ tin: LLM ≥ 0,9, luật ≥ 0,75), Trust Engine (≥ 20 mẫu, ≥ 0,85) — trượt một cổng là escalate. **Chưa có:** số OSD/PG ảnh hưởng, rollback *đã diễn tập*, bất đồng ensemble.
- [x] Ngân sách FRR `SHADOW_FRR_BUDGET` (mặc định 5%): FRR = tỉ lệ khuyến nghị execute của chính policy trong 30 ngày bị operator đánh giá sai (cần ≥ 10 nhãn mới áp dụng); vượt → escalate. Hạ quyền thật + cảnh báo làm cùng WP6.4 (hiện chưa có gì để hạ).
- [ ] Tích hợp: đầu ra chỉ là **một input** cho `autopilot_guardrails` + Trust Engine — **cố ý chưa nối**: hiện policy chỉ ghi, không có đường nào từ shadow tới thực thi (test chứng minh). Báo cáo tuần hiển thị số execute/escalate và lý do chính.

### WP6.4 Lộ trình nâng quyền

- [ ] Shadow ≥ 30 ngày, OPE DR cho thấy FRR ≤ ngân sách với CI 95% → đề xuất canary cho **1 action × 1 cluster**, có operator approval ghi audit (như promotion model registry).
- [ ] Canary 14 ngày, theo dõi FRR thật, regressed_24h, số escalation; tự rollback về APPROVAL_REQUIRED khi vi phạm.
- [ ] Test: guardrail vẫn chặn khi kill switch bật dù bandit chọn execute; vi phạm FRR tự hạ quyền.

**Nghiệm thu WP6:** ≥ 1 action chạy LIMITED_AUTOPILOT trên 1 cluster 14 ngày với FRR ≤ ngân sách, không incident do automation.
**Ước lượng:** 8–10 ngày + thời gian shadow/canary.

---

## WP7 — Online learning forecasting (tiếp nối, ưu tiên P1)

- [x] Sau deploy `0670de30` (05/10): **không còn `update_failed`** từ 29/09. Nhưng **`applied = 0` ở mọi chu kỳ từ 19/09** (6.313 mẫu, 1.592 nhãn `READY` khớp đúng mẫu, 0 nhãn dùng): watcher đưa mỗi mẫu vào một lần lúc quan sát, nhãn tới sau ~13 giây, nhánh học chỉ chạy khi cùng `sample_id` được đưa lại. Sửa: `apply_ready_labels()` (`5c3f9ca4`) + chỉ quét phạm vi canary (`cf1f2219`, lượt đầu tiêu hết ngân sách 5 s/3 mẫu cho host ngoài canary). **Xác nhận 05/10 sau deploy `cf1f2219`: 2 mẫu `10.20.1.153`/cpu `update_applied=true`, 2 nhãn `CONSUMED`, state `river_mean` sample_count=2 (SHADOW_ONLY).** Tồn ~135 nhãn cpu của canary, ~3 nhãn/lượt quét 15 phút.
- [x] Tự động hằng tuần: báo cáo AI Ops thứ Hai chạy `shared/river_v2_evidence.build_report` (lõi tách từ script), thêm 2 dòng Telegram mỗi cluster (verified · đã học, chênh lệch so với tuần trước; verdict nâng cấp) và lưu `river-v2-evidence-<năm>-W<tuần>.json` vào `learning_evidence_report_dir` (mặc định `/var/lib/ceph-ai/learning-evidence`, ngoài repo vì có tên host). Mốc W41 (05/10): verified 1.663, scored 6, `KEEP_SHADOW`.
- [ ] Khi ≥ 20 verified outcome/scope ở ≥ 3 scope: mở rộng canary online learning từ 1 host/cpu sang 1 cluster (cpu+memory), vẫn SHADOW_ONLY.
- [ ] Theo dõi `paired_holdout` (`scripts/river_linear_v2_runtime_replay.py`): chỉ đề xuất promotion khi verdict `CANDIDATE_BETTER` ổn định ≥ 2 tuần.
- [ ] Gắn sự kiện forecast vào WP6 như feature (dự báo đầy disk/CPU).

**Ước lượng:** 1 ngày công + theo dõi định kỳ.

---

## WP8 — Quan sát, quản trị và tài liệu

- [ ] Trang `/ai-learning`: thẻ KPI (WP0), chất lượng LF (WP2.3), evidence coverage (WP3), OPE/FRR (WP6).
- [x] Báo cáo tuần tự động: mục "🤖 Tự vận hành" ghép vào digest AI Ops thứ Hai sẵn có (`worker/ai_ops_digest.py`, lịch `AI_OPS_WEEKLY_DIGEST_*`), mỗi cluster: nhiễu (incident, top family, tỉ lệ mở lại, investigate_manually), verdict mới + tiến độ tới 200 nhãn, bằng chứng + độ phủ luật, nguồn quyết định, playbook đủ/gần ngưỡng Trust Engine (`shared/weekly_autonomy_report.py`). Mỗi mục lỗi riêng (rollback, ghi "Thiếu mục") không chặn digest. JSON: `AI_OPS_WEEKLY_REPORT_DIR` (mặc định tắt) hoặc `scripts/weekly_autonomy_report.py` chạy tay. Chạy thật 28/09 (`docs/benchmark/weekly-autonomy-2026-W40.json`): 1.842 incident/7 ngày, mở lại ≤30 phút 60,2%, investigate_manually 97,2%, 3/200 verdict; evidence/decisions báo thiếu vì production chưa migrate.
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
| 28/09/2026 | WP2.1 Verdict Telegram | Hỏi verdict sau mỗi quyết định, lý do chọn sẵn, hàm ghi dùng chung, audit | `tests/test_telegram_approval_bot.py` 47 passed | Partial (chờ deploy, đo ≥ 30 verdict/tuần) |
| 28/09/2026 | WP1.2 điều chỉnh | BlueStore = bão lịch sử 06/09 (đã chặn); OSD latency mở 4/đóng 3 scan, replay 256 → 48 (−81%); thêm WP1.4 gom CRUSH skew | `tests/test_osd_latency_monitor.py` 16 passed | Partial (chờ deploy) |
| 28/09/2026 | WP1.4 CRUSH skew | 1 incident/tín hiệu thay vì 1/entity; replay 369 → 66 (−82%) | `tests/test_crush_skew_monitor.py` 29 passed | Partial (chờ deploy) |
| 28/09/2026 | WP2.2 Nhắc verdict | 5 case/ngày theo độ hữu ích, qua kênh cluster, không trùng | 54 passed | Partial (chờ deploy) |
| 28/09/2026 | WP2.3 Nhãn tự động | 6 LF, tính khi cần (không migration), báo cáo chỉ đọc; 92% case có nhãn | 5 passed | Done (precision chờ nhãn operator) |
| 28/09/2026 | WP2.4 Verdict trên Alert Center | Cột verdict + nút một chạm + lọc chưa nhãn; check trình duyệt | 5 passed | Done (chờ deploy) |
| 28/09/2026 | WP4 Tính năng Ceph (chỉ đọc) | Card Disk Risk: devicehealth, diskprediction_local, pg_autoscaler, balancer + SMART; phân biệt smartctl lỗi; action bật module hoãn (đĩa ảo) | 14 passed | Partial (chờ deploy; action + ingest định kỳ còn lại) |
| 28/09/2026 | WP3.1 Evidence collectors | 15 collector chỉ đọc, chặn lệnh ghi 2 lớp, ngân sách/breaker/cooldown; thử thật 11/11 ok | 28 passed | Done (chưa gắn vào incident — WP3.3) |
| 28/09/2026 | WP3.2 Runbook điều tra | 12 runbook + default, validator đồng bộ với registry; chạy thật OSD_LATENCY_HIGH 5/5 ok trong 37 s | 22 passed | Done |
| 28/09/2026 | WP3.3 Tự thu evidence khi incident mở | Scan phụ nền, bảng incident_evidence + migration, hiển thị timeline; Telegram + cluster quan sát còn lại | 8 passed (135 cùng watcher/migrations) | Partial (chờ deploy + migrate) |
| 28/09/2026 | WP3.4 Chẩn đoán theo luật | 3 family, trích dẫn evidence, UNKNOWN khi thiếu; ghi timeline; chưa gắn cổng LLM | 24 passed | Partial |
| 28/09/2026 | WP3.4 Cổng evidence trước LLM | Worker tự thu evidence khi chưa có; luật tự tin thay LLM; LLM nhận [E#] khi UNKNOWN; tóm tắt vào Telegram | 5 test mới, 294 passed | Done (chờ deploy + đo hallucination) |
| 28/09/2026 | Review 6,2/10 — hysteresis qua restart | Streak recovery `NODE_UNREACHABLE` nằm trong RAM: sau restart, 1 probe tốt đã đóng incident (test đặt tên ngược). Sửa fail-closed: Watcher mới khởi động phải tự thấy đủ N probe tốt; `OSD_LATENCY_HIGH` vốn đã fail-closed | test_node_health_monitor 22 passed | Done |
| 28/09/2026 | Review — deadline cứng | Timeout collector không truyền xuống Ceph transport và ngân sách 60 s chỉ kiểm tra trước mỗi collector. Nay mỗi collector chờ tối đa min(timeout, ngân sách còn lại) trong thread riêng; slot host giữ tới khi lệnh thật sự kết thúc | 2 test mới | Done |
| 28/09/2026 | Review — bật mặc định quá sớm | `INVESTIGATION_ENABLED` mặc định **false**; `INVESTIGATION_CLUSTER_IDS` giới hạn cluster canary; cổng worker (mọi cluster) và scanner (cluster mặc định) cùng tôn trọng | 1 test | Done |
| 28/09/2026 | Review — dữ liệu evidence | Retention `INCIDENT_EVIDENCE_RETENTION_DAYS=30` trong sweep `learning_retention`; output thô trên timeline chỉ admin xem; mỗi collector ≤ 6.000 ký tự (≤ ~90 KB/incident). Chưa có mã hoá riêng | 2 test | Partial |
| 28/09/2026 | Review — weak supervision | LF 'tự hết không action' và 'host chập chờn' không còn gắn `FALSE_POSITIVE` mà là lớp riêng `SELF_RESOLVED`/`FLAPPING`; precision so với `FALSE_POSITIVE` của operator chỉ là proxy. Dữ liệu thật đầu tiên: 3 verdict operator (`NODE_RESOURCE_HIGH`, đều `CORRECT`) trùng case LF gắn 'tự hết' → proxy precision 0/3 — xác nhận heuristic dễ hiểu sai; chưa đủ 20 mẫu để quyết trọng số | `docs/benchmark/auto-labels-2026-09-28.json` | Done |
| 28/09/2026 | Review — release_gate | `release_gate` nay cần `integration` thành công. Còn lại: image vẫn được build/push trong job `quality` trước khi integration xong | workflow | Partial |
| 28/09/2026 | Review — artifact CS-LAB | `scripts/evidence_smoke.py` (chỉ đọc, không lưu output lệnh, che IP) → `docs/benchmark/evidence-smoke-2026-09-28.json`: 5 runbook, 19/19 collector ok, lượt dài nhất 16,7 s; triage: TRANSIENT, RECOVERED×2, UNKNOWN×2 | artifact | Done |
| 28/09/2026 | Review — chưa làm được bằng code | Deploy canary + đo KPI ≥ 7 ngày; ≥ 100 verdict; deploy/live workflow trên runner đúng; PostgreSQL/RabbitMQ/rollback/soak/DR; HA | — | Chờ vận hành |
| 28/09/2026 | WP6.1–6.2 Decision log + OPE | Worker ghi quyết định (nguồn, context có cấu trúc, propensity) cho mọi case mới; IPS/SNIPS/DR + CI + support; báo cáo chỉ đọc. Học/exploration (WP6.3) vẫn chờ ≥ 200 nhãn | 17 test mới, 212 passed | Done (chờ deploy + migrate) |
| 28/09/2026 | WP8 Báo cáo tuần | Mục autonomy trong digest thứ Hai + JSON tuỳ chọn + script chạy tay; mỗi mục hạ cấp độc lập (đã thấy trên PostgreSQL thật khi thiếu bảng) | 4 test mới | Done (chờ deploy) |
| 28/09/2026 | WP6.3 Chính sách shadow execute/escalate | Luật ràng buộc rủi ro gắn khuyến nghị + lý do vào mỗi quyết định, FRR theo cluster, savepoint để lỗi không ảnh hưởng chẩn đoán; báo cáo tuần có mục shadow | 17 test mới, 183 passed | Done (shadow; bandit học chờ ≥ 200 nhãn) |
| 28/09/2026 | Actor audit | Cắt actor về VARCHAR(32) ở audit/timeline (luồng Duyệt cũ có thể fail trên PostgreSQL) | `6b924ba9` | Accepted |
| 28/09/2026 | Gate mypy | FORCE_COLOR làm budget/quality gate đọc 0 lỗi mypy; sửa + fail-closed | budget 827/152 | Accepted |
| 02/10/2026 | Sự cố — không có incident health check | Cụm mặc định không tạo incident từ health check (OSD_DOWN, PG_DEGRADED, BLUESTORE…) từ 15/09: `watcher.main` giao việc này cho `watcher.remediation_main` nhưng Compose không có service đó; tiến trình `remediation_main` sót từ test (SQLite + MON giả) còn giữ khoá. Thêm service `remediation-watcher` | `fbceb4dc` | Done (chờ deploy) |
| 02/10/2026 | Cảnh báo mất health | Watcher mất health cả cụm 2,5 giờ (MON leader quá tải) mà không ai được báo; nay mở `CEPH_HEALTH_UNAVAILABLE` sau 10 phút, tự đóng khi đọc lại được | `81fe97ee` | Done (chờ deploy) |
| 02/10/2026 | WP1.2 BlueStore | Mẫu + bộ lọc xu hướng + action tune có rollback + nối runbook WP3; replay 1.040 → 8 (bằng mức của unique index) | `4c788fe3`, `49b6cba3`, replay JSON | Done (chờ deploy + migrate) |
| 03/10/2026 | WP1.3 Reopen chung | Trì hoãn đóng incident chung 30 phút; replay 26 giờ: mở lại 74 → 19, incident 135 → 61 | `b8250a2d`, 6 test | Done (chờ deploy + đo 7 ngày) |

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
