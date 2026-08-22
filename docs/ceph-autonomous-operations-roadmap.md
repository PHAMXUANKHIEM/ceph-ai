# Ceph AI — Lộ trình vận hành tự chủ và học từ remediation

Cập nhật: **2026-08-22**.

Tài liệu này mô tả lộ trình đưa `ceph-ai` từ hệ thống phát hiện, chẩn đoán và
đề xuất hành động thành một vòng vận hành khép kín có thể tự xử lý các lỗi đã
được kiểm chứng. Mục tiêu không phải trao shell tự do cho mô hình, mà là giảm
dần sự phụ thuộc vào phê duyệt thủ công bằng evidence, playbook đóng và kết quả
thực tế đã được xác minh.

## 1. Mục tiêu và ranh giới

### 1.1. Mục tiêu

- Tự phát hiện và tương quan health, log, metric và tín hiệu thiết bị.
- Nhận diện lỗi mới hoặc lỗi đã từng xử lý thành công.
- Chọn remediation từ catalogue phía server, không sinh shell tùy ý.
- Tự thực thi action đủ an toàn và đủ độ tin cậy.
- Xác minh kết quả bằng telemetry thay vì chỉ dựa vào exit code.
- Lưu lại toàn bộ ca xử lý để đánh giá và tái sử dụng cho lần sau.
- Tự hạ quyền, rollback hoặc chuyển cho người vận hành khi có bất thường.
- Tăng dần tỷ lệ lỗi quen thuộc được xử lý mà không cần người xác nhận.

### 1.2. Không thuộc mục tiêu

- Không cho LLM SSH trực tiếp hoặc tự xây command string.
- Không để LLM tự sửa allowlist, policy, threshold hoặc cấp quyền cho chính nó.
- Không tự động hóa thao tác có khả năng làm mất dữ liệu chỉ vì confidence cao.
- Không coi câu trả lời giống ca cũ về ngữ nghĩa là đủ để chạy remediation.
- Không fine-tune trực tiếp từ mọi kết quả production chưa được xác minh.
- Không loại bỏ kill switch, audit hoặc trách nhiệm của operator.

Đích thực tế là: lỗi quen thuộc có blast radius thấp được tự xử lý; con người
chỉ tham gia khi lỗi mới, evidence yếu, hành động rủi ro hoặc playbook suy giảm
chất lượng.

## 2. Nền tảng hiện có

`ceph-ai` hiện đã có phần lớn khung closed-loop:

1. Watcher phát hiện `ceph health detail` và các tín hiệu bổ sung.
2. Incident được publish cho Worker.
3. AI chẩn đoán bằng structured output.
4. `action_id` phải qua allowlist và policy gate.
5. Action được phân loại `READ_ONLY`, `SAFE`, `RISKY`, `DESTRUCTIVE`.
6. `SAFE` có command builder đóng và có thể tự chạy.
7. `RISKY` chuyển sang `PENDING_APPROVAL` trên Dashboard/Telegram.
8. `DESTRUCTIVE` bị chặn khỏi luồng tự động.
9. Sau khi chạy, Incident chuyển sang `VERIFYING`.
10. Watcher chỉ kết luận thành công khi health/telemetry xác nhận lỗi đã hết.

Các phần còn thiếu chủ yếu là case memory có cấu trúc, trust engine, autonomy
gate, shadow evaluation, rollback chuẩn hóa và cơ chế nâng/hạ maturity dựa trên
số liệu.

## 3. Quy trình vận hành khép kín

```text
Detect
  → Collect evidence
  → Correlate and identify fault
  → Retrieve verified cases/playbook
  → Diagnose
  → Select action_id
  → Deterministic preflight
  → Autonomy decision
      ├─ observe/recommend
      ├─ request approval
      └─ auto-execute
  → Execute under lock and rate limit
  → Verify with fresh telemetry
      ├─ resolved
      ├─ inconclusive
      └─ failed/regressed
  → Rollback or escalate
  → Record outcome
  → Recalculate playbook trust
  → Promote, retain or demote autonomy
```

### 3.1. Detect

Watcher tạo Incident từ tín hiệu phía server, không từ kết luận tự do của AI.
Mỗi Incident cần có:

- `cluster_id` và timestamp.
- Health code hoặc fault family.
- Entity đã chuẩn hóa: host, daemon, OSD, pool, volume hoặc device.
- Evidence snapshot bất biến tại thời điểm phát hiện.
- Ceph version và deployment mode nếu truy vấn được.
- Severity, freshness và provenance của từng evidence.

### 3.2. Correlate

Correlation ưu tiên khóa tất định:

- Cùng cluster.
- Cùng fault family.
- Cùng entity hoặc quan hệ topology đã được server xác nhận.
- Cửa sổ thời gian giao nhau.
- Evidence còn mới.

Vector similarity chỉ dùng để tìm case tham khảo, không được thay thế điều kiện
identity và safety.

### 3.3. Diagnose

AI nhận một evidence envelope đóng và trả structured output:

```json
{
  "fault_family": "BLUESTORE_SLOW_OP",
  "target_entities": ["osd.12"],
  "root_cause_hypothesis": "OSD daemon is stalled",
  "confidence": 0.96,
  "recommended_action_id": "restart_osd_daemon",
  "citations": ["evidence:health-123", "evidence:log-456"]
}
```

Server phải kiểm tra lại fault family, target, citation, action allowlist và
khả năng tương thích. Không tin trực tiếp entity hoặc classification do model
trả về.

### 3.4. Preflight

Preflight là code tất định, chạy lại ngay trước execution:

- Cluster và target vẫn active.
- Evidence chưa hết hạn và lỗi vẫn tồn tại.
- Target tồn tại duy nhất và thuộc đúng cluster.
- Không có upgrade, restore, patch hoặc remediation khác xung đột.
- Cluster không recovery/rebalance vượt ngưỡng cấu hình.
- Quorum, redundancy và failure domain còn đủ an toàn.
- Action chưa vượt rate limit/cooldown.
- Command builder và post-check tồn tại.
- Rollback tồn tại nếu maturity yêu cầu rollback.

Preflight thất bại phải tạo audit event và dừng action; AI không được phép giải
thích để bỏ qua rule.

### 3.5. Autonomy decision

Autonomy Gate quyết định bằng policy phía server:

```text
decision = min(
  cluster autonomy level,
  playbook maturity,
  action safety ceiling,
  evidence confidence gate,
  current operational safety gate
)
```

Kết quả chỉ thuộc một trong các trạng thái:

- `OBSERVE_ONLY`
- `RECOMMEND_ONLY`
- `PENDING_APPROVAL`
- `AUTO_EXECUTE_AFTER_GRACE`
- `AUTO_EXECUTE`
- `BLOCKED`

### 3.6. Execute

- Lấy distributed lock theo cluster và conflict scope.
- Chụp evidence/pre-state lần cuối.
- Render lệnh từ command builder đóng.
- Ghi command preview và checksum vào audit.
- Chạy bằng credential tối thiểu cần thiết.
- Giới hạn timeout, output và số target.
- Ghi exit status nhưng chưa đánh dấu đã sửa xong.
- Chuyển Incident sang `VERIFYING`.

### 3.7. Verify

Post-check phải độc lập với câu trả lời AI và dùng telemetry mới:

- Health code đã biến mất hay chưa.
- Daemon/OSD/service đã trở lại trạng thái mong đợi hay chưa.
- PG, client I/O, latency và recovery có xấu đi không.
- Có xuất hiện fault family mới sau action không.
- Lỗi có tái diễn trong các cửa sổ 1 giờ, 24 giờ và 7 ngày không.

Exit code 0 chỉ có nghĩa command đã chạy, không có nghĩa remediation thành công.

### 3.8. Rollback và escalate

- Nếu post-check thất bại và rollback đã được kiểm chứng, chạy rollback.
- Nếu rollback không tồn tại, chuyển `PENDING_APPROVAL` hoặc `ESCALATED`.
- Khóa tự động playbook/target khi có failure nghiêm trọng.
- Đính kèm evidence trước/sau và lý do thất bại vào Telegram/Dashboard.
- Không retry vô hạn; mọi retry phải chịu rate limit và cooldown.

## 4. Remediation Case Memory

Mỗi lần xử lý tạo một bản ghi case bất biến về đầu vào và kết quả. Case không
được coi là dữ liệu học thành công cho đến khi Outcome Evaluator hoàn tất.

### 4.1. Trường dữ liệu đề xuất

`remediation_cases`:

- `id`, `incident_id`, `action_id`, `cluster_id`.
- `fault_family`, `entity_keys`, `evidence_fingerprint`.
- `ceph_version`, `deployment_mode`, `topology_snapshot`.
- `diagnosis`, `diagnosis_confidence`, `prompt_version`, `model_provider`.
- `classification`, `autonomy_decision`, `playbook_version`.
- `preflight_snapshot`, `command_preview_hash`.
- `approved_by`, `approval_source`, `approval_latency_seconds`.
- `started_at`, `executed_at`, `verified_at`.
- `pre_state`, `post_state`, `rollback_state`.
- `outcome`, `recovery_seconds`, `side_effects`.
- `regressed_1h`, `regressed_24h`, `regressed_7d`.
- `operator_verdict`, `operator_note`.

`playbook_stats`:

- `playbook_id`, `playbook_version`, `scope_key`.
- Tổng số proposed, approved, rejected, executed và verified.
- Success, failure, rollback và inconclusive count.
- False-positive count.
- Success rate theo cửa sổ 30/90 ngày.
- Mean/p95 recovery time.
- Confidence calibration error.
- `trust_score`, `maturity_level`, `last_failure_at`.
- `auto_disabled_reason`, `promotion_candidate_at`.

`scope_key` tối thiểu phải phân biệt Ceph major version và deployment mode.
Không gộp kết quả cephadm với package deployment nếu execution khác nhau.

### 4.2. Evidence fingerprint

Fingerprint được tạo phía server từ:

- Fault family.
- Entity chuẩn hóa.
- Các health code liên quan.
- Tập log template đã redaction.
- Bucket metric đã lượng tử hóa.
- Ceph major version và deployment mode.

Không hash raw log chứa timestamp/UUID động vì sẽ làm mọi incident thành một
case khác nhau.

### 4.3. Tìm case tương tự

Thứ tự tìm kiếm:

1. Exact fault family + entity type + deployment mode + Ceph major version.
2. Evidence fingerprint hoặc tập feature tất định tương thích.
3. Semantic similarity trên diagnosis/log summary để xếp hạng phụ.
4. Loại bỏ case chưa verify, case rollback thất bại và case quá cũ sau thay đổi lớn.

Case tương tự giúp AI giải thích và chọn playbook; policy vẫn quyết định có
được tự chạy hay không.

## 5. Maturity và mức tự chủ

Mỗi playbook có maturity độc lập, không tồn tại một nút “AI hoàn toàn tự chủ”
cho mọi hành động.

| Cấp | Tên | Hành vi |
|---|---|---|
| L0 | Observe | Chỉ phát hiện, correlation và ghi evidence |
| L1 | Recommend | AI chẩn đoán và đề xuất, không tạo executable action |
| L2 | Human approved | Tạo action nhưng bắt buộc người duyệt |
| L3 | Auto-safe | Tự chạy action SAFE sau grace period |
| L4 | Auto-rollback | Tự chạy, tự verify và rollback đã kiểm chứng |
| L5 | Autonomous | Tự xử lý, con người nhận báo cáo và có quyền can thiệp |

### 5.1. Trần tự chủ theo safety class

- `READ_ONLY`: tối đa L5.
- `SAFE`: tối đa L5 nếu blast radius và rollback phù hợp.
- `RISKY`: mặc định tối đa L2; chỉ action cụ thể được admin cấp ngoại lệ mới có
  thể lên L3/L4.
- `DESTRUCTIVE`: tối đa L2 và luôn cần xác nhận mạnh; không được auto-execute.

Confidence của model không thể nâng trần safety class.

### 5.2. Điều kiện đề xuất nâng L2 → L3

Giá trị mặc định ban đầu:

```text
verified_cases >= 10
verified_success_rate_30d >= 0.98
false_positive_rate_30d <= 0.01
rollback_rate_30d <= 0.02
consecutive_verified_successes >= 5
confidence_calibration_error <= 0.10
target_is_deterministic = true
preflight_available = true
postcheck_available = true
recent_severe_failures = 0
```

Đạt điều kiện chỉ tạo `PROMOTION_CANDIDATE`; lần nâng quyền đầu tiên phải do
admin phê duyệt. Sau khi tổ chức đã có quy trình governance ổn định, có thể cho
phép tự nâng trong một trần đã được admin cấu hình trước.

### 5.3. Điều kiện hạ cấp tự động

Hạ ngay về L2 hoặc thấp hơn khi:

- Một severe side effect.
- Rollback thất bại.
- Target ambiguity.
- Ba lần verify thất bại liên tiếp.
- Success rate cửa sổ 30 ngày xuống dưới ngưỡng.
- Ceph major version hoặc deployment mode thay đổi.
- Playbook/command builder đổi version đáng kể.
- Operator bấm kill switch hoặc đánh dấu action không an toàn.

Hạ cấp không cần LLM và không cần chờ phê duyệt.

## 6. Trust Engine

Trust score phải xuất phát từ outcome đã verify, không từ số lần AI tự tin.

Một công thức khởi đầu có thể dùng:

```text
base = wilson_lower_bound(successes, executions)
trust = base
        - 0.35 * severe_side_effect_rate
        - 0.20 * rollback_rate
        - 0.15 * false_positive_rate
        - 0.10 * confidence_calibration_error
```

Sau đó áp dụng hard gate ở mục 5; trust score không được vượt qua một hard
gate thất bại. Wilson lower bound giúp tránh tình trạng 1/1 lần thành công đã
được coi là 100% đáng tin.

Thống kê cần tách theo:

- Playbook version.
- Ceph major version.
- Deployment mode.
- Entity type.
- Cluster tier: lab, staging, production.

Dữ liệu lab có thể giúp vào shadow mode nhưng không tự động cấp quyền production.

## 7. Shadow Autopilot

Trước khi auto-execute, AI chạy chế độ shadow trong tối thiểu 2–4 tuần hoặc đủ
số case cấu hình:

1. AI đưa ra quyết định như thể được tự vận hành.
2. Hệ thống không thực thi quyết định shadow.
3. Operator vẫn xử lý theo quy trình hiện tại.
4. Outcome Evaluator so sánh action/target/timing của AI với kết quả thực.
5. Dashboard hiển thị precision, false positive và missed remediation.

Chỉ số bắt buộc:

- Diagnosis precision.
- Target precision.
- Action agreement với action cuối cùng được duyệt.
- Estimated avoided downtime.
- Unsafe proposal count.
- Cases mà AI muốn chạy nhưng preflight đúng ra phải chặn.

Unsafe proposal count phải bằng 0 trong cửa sổ đánh giá trước khi mở L3.

## 8. Guardrail runtime

### 8.1. Lock và xung đột

- Tối đa một write action trên một cluster tại một thời điểm.
- Lock nhỏ hơn theo OSD/host chỉ dùng khi chứng minh được không chung failure domain.
- Upgrade, restore, patch, rebalance thủ công và cluster lifecycle khóa toàn cluster.
- Lock có TTL, heartbeat và audit owner; không phá lock chỉ vì timeout cục bộ.

### 8.2. Rate limit và cooldown

Mặc định production:

- Tối đa 2 auto-remediation/cluster/giờ.
- Tối đa 5 auto-remediation/cluster/ngày.
- Không lặp cùng `action_id + target` trong 30 phút.
- Không restart cùng daemon quá 2 lần trong 24 giờ.
- Không thao tác đồng thời nhiều daemon cùng failure domain.

### 8.3. Operational gates

Chặn tự động khi:

- Mất quorum hoặc không đọc được health đáng tin cậy.
- Cluster đang `HEALTH_ERR`, trừ playbook được thiết kế chính xác cho health code đó.
- PG inactive/incomplete vượt ngưỡng.
- Recovery/backfill/client latency vượt ngưỡng.
- Evidence cũ hoặc telemetry source bị gián đoạn.
- Clock skew làm timeline không đáng tin cậy.
- Credential, inventory hoặc capability snapshot chưa được xác minh.

### 8.4. Kill switch

Cần ba tầng:

- Global: dừng toàn bộ auto-execution.
- Per cluster: dừng tự động trên một cluster.
- Per playbook: khóa một remediation cụ thể.

Kill switch chỉ chặn action mới; action đang chạy phải đi theo cancel/rollback
contract riêng, không kill process mù quáng.

## 9. Notification và quyền can thiệp

Ở L3, Telegram/Dashboard gửi thông báo trước action và có grace period mặc định
60 giây:

```text
AI chuẩn bị tự xử lý BLUESTORE_SLOW_OP trên osd.12.
Playbook trust: 99.1% từ 18 ca đã verify.
Preflight: PASS. Blast radius: 1 OSD.
Tự chạy sau 60 giây — [Dừng] [Yêu cầu duyệt thủ công]
```

Ở L4/L5 có thể bỏ grace period cho lỗi cần phản ứng nhanh, nhưng vẫn gửi event
ngay khi bắt đầu. Thông báo sau action phải phân biệt:

- Command completed.
- Verification passed.
- Verification inconclusive.
- Rolled back.
- Escalated.

Không dùng chữ “đã khắc phục” trước khi verification pass.

## 10. Học sau remediation

### 10.1. Outcome windows

- Tức thời: command và post-check cơ bản.
- 1 giờ: lỗi có tái xuất hiện nhanh không.
- 24 giờ: regression, restart loop hoặc metric xấu hơn không.
- 7 ngày: dùng cho promotion và reliability report.

Case chỉ đạt `VERIFIED_STABLE` khi hoàn thành cửa sổ được playbook yêu cầu.

### 10.2. Nội dung được phép học

- Playbook nào hiệu quả với fault/evidence nào.
- Confidence calibration.
- Thời gian phục hồi và xác suất tái phát.
- Ngưỡng anomaly có tạo false positive không.
- Điều kiện preflight nào dự báo thất bại tốt.

### 10.3. Nội dung không tự học/thay đổi

- Safety classification.
- Command catalogue.
- SSH privilege.
- Destructive allowlist.
- Trần blast radius.
- Quy tắc quorum/data safety.

Các thay đổi trên chỉ được tạo dưới dạng đề xuất có diff, test và phê duyệt.

## 11. Playbook contract

Mỗi playbook tự động phải khai báo:

```yaml
id: restart_osd_daemon
version: 1
supported_fault_families:
  - BLUESTORE_SLOW_OP
safety_class: SAFE
max_autonomy: L4
target_schema: osd
preflight: preflight_restart_single_osd
execute: command_restart_osd_daemon
postcheck: verify_osd_and_health_code
rollback: null
blast_radius:
  max_osds: 1
  max_hosts: 1
cooldown_seconds: 1800
required_capabilities:
  - osd_inventory
  - health_detail
```

Nếu thiếu preflight, post-check hoặc target schema thì playbook tối đa L2.

## 12. Data/state machine đề xuất

Action lifecycle mở rộng:

```text
PROPOSED
  → PREFLIGHT_PENDING
  → PREFLIGHT_BLOCKED | SHADOWED | PENDING_APPROVAL
  → GRACE_PERIOD | APPROVED
  → EXECUTING
  → VERIFYING
  → VERIFIED | INCONCLUSIVE | FAILED
  → ROLLING_BACK
  → ROLLED_BACK | ROLLBACK_FAILED
  → ESCALATED
```

Không sửa nghĩa các trạng thái cũ đột ngột. Migration nên bổ sung event ledger
và mapping tương thích trước, sau đó mới mở state mới trên UI.

Audit event tối thiểu:

- `AUTONOMY_DECISION_MADE`
- `PLAYBOOK_CASE_MATCHED`
- `PREFLIGHT_PASSED/BLOCKED`
- `AUTO_ACTION_GRACE_STARTED/CANCELLED`
- `AUTO_ACTION_EXECUTED`
- `POSTCHECK_PASSED/FAILED/INCONCLUSIVE`
- `ROLLBACK_STARTED/PASSED/FAILED`
- `PLAYBOOK_PROMOTION_PROPOSED/APPROVED`
- `PLAYBOOK_AUTO_DEMOTED`
- `AUTOPILOT_KILL_SWITCH_CHANGED`

## 13. Cấu hình đề xuất

Global defaults và per-cluster override:

```env
AUTOPILOT_ENABLED=false
AUTOPILOT_DEFAULT_LEVEL=L2
AUTOPILOT_GRACE_SECONDS=60
AUTOPILOT_MAX_ACTIONS_PER_HOUR=2
AUTOPILOT_MAX_ACTIONS_PER_DAY=5
AUTOPILOT_TARGET_COOLDOWN_SECONDS=1800
AUTOPILOT_MIN_DIAGNOSIS_CONFIDENCE=0.90
AUTOPILOT_MIN_VERIFIED_CASES=10
AUTOPILOT_MIN_SUCCESS_RATE=0.98
AUTOPILOT_MAX_FALSE_POSITIVE_RATE=0.01
AUTOPILOT_AUTO_PROMOTION_ENABLED=false
AUTOPILOT_SHADOW_ENABLED=true
```

Secret và credential không được lưu trong case/evidence hoặc gửi vào model.

## 14. Dashboard cần bổ sung

### 14.1. Autopilot Settings

- Global/per-cluster level.
- Shadow mode.
- Grace period, rate limit và maintenance window.
- Kill switch.
- Danh sách playbook và maturity hiện tại.
- Lý do playbook chưa đủ điều kiện tự chạy.

### 14.2. Autonomy Dashboard

- Tỷ lệ incident tự xử lý.
- Verified success/rollback/false-positive rate.
- Human approval saved và approval override.
- Playbook promotion/demotion timeline.
- Top recurring faults và recovery time.
- Shadow decisions so với outcome thực.

### 14.3. Incident timeline

Hiển thị đầy đủ detect → evidence → diagnosis → case match → preflight → autonomy
decision → execution → verify → outcome. Mỗi kết luận AI phải dẫn tới evidence ID.

## 15. Lộ trình triển khai

### Pha 0 — Baseline và invariant

Trạng thái triển khai (2026-08-22): **baseline đã hoàn thành**.

- Đã thêm global kill switch `AUTOPILOT_ENABLED=false` mặc định. Khi tắt,
  action `SAFE` được giữ ở `PENDING_APPROVAL`, không chạy SSH.
- Đã bật preflight enforcement mặc định và kiểm tra lại preflight ngay tại
  execution boundary để đóng race giữa diagnosis và execution.
- Đã thêm audit `autopilot_kill_switch_blocked` và test chứng minh kill switch
  chặn SSH, unknown/stale capability fail closed.
- Đã có post-execution `VERIFYING` bằng telemetry mới; vẫn cần chuẩn hóa
  post-check theo từng playbook ở Pha 2.
- Đã thêm operational gate từ `ceph status` mới: HEALTH_ERR, MON quorum,
  PG inactive/incomplete/stale, recovery threshold và OSD latency incident.
- Đã thêm lease write action duy nhất theo cluster (có TTL), giới hạn 2 action/
  giờ, 5 action/ngày và cooldown action+target 30 phút.
- Đã có UI admin cho global kill switch với xác nhận mạnh khi bật, lý do bắt
  buộc, audit append-only và fail-safe dừng Worker cũ nếu thao tác tắt không
  thể khởi động Worker mới.
- Đã có fault-injection chứng minh lease hết TTL sau Worker crash có thể được
  thu hồi, trong khi lease chưa hết hạn vẫn loại trừ action thứ hai.
- Đã xử lý trường hợp database commit lỗi/Worker chết sau khi SSH có thể đã
  chạy: action `EXECUTING` quá TTL chuyển sang `INCONCLUSIVE`, ghi audit và
  timeline evidence, giải phóng lease nhưng tuyệt đối không tự chạy lại.
- Đã có fault-injection cho database commit lỗi sau SSH, Worker recovery và
  RabbitMQ redelivery; test chứng minh lệnh chỉ được dispatch đúng một lần.
- Freshness/capability evidence fail closed đã có trong preflight; audit chi
  tiết từng evidence và post-check riêng từng playbook được tiếp tục ở Pha 2.

- Chốt state machine hiện tại bằng test.
- Đo success/failure của SAFE action đang chạy.
- Xác minh audit, redaction và freshness guard.
- Thêm global kill switch mặc định tắt Autopilot.

Điều kiện hoàn thành: không có action nào bỏ qua policy/preflight/post-check.

### Pha 1 — Remediation Case Memory

Trạng thái triển khai (2026-08-22): **đang thực hiện**.

- Đã thêm schema `remediation_cases` và `playbook_stats`, tách scope theo
  playbook version để chuẩn bị cho Trust Engine.
- Action do pipeline AI tạo case trong cùng transaction; case đóng băng
  redacted pre-state, entity, deployment mode, Ceph version và evidence
  fingerprint tất định.
- Execution thành công chỉ ghi `EXECUTED_PENDING_VERIFY`. Telemetry mới mới
  được phép kết luận `VERIFIED_SUCCESS`/`VERIFIED_FAILED`; recovery không
  xác định ghi `INCONCLUSIVE` và không retry.
- Đã có reconciler theo lô cho Action lịch sử và các pipeline ngoài AI. Case
  `RESOLVED` cũ chỉ được gắn `LEGACY_RESOLVED_UNVERIFIED`, không đủ điều kiện
  học trust vì lịch sử đó có thể chỉ dựa vào SSH exit code.
- Đã có Outcome Evaluator tất định cho recurrence 1h/24h/7d theo đúng
  cluster + fault family + entity. Regression được ghi `true` ngay khi có
  bằng chứng; `false` chỉ được ghi sau khi cửa sổ tương ứng đã đóng.
- Chưa hoàn tất: UI operator verdict.

- Thêm migration và model `remediation_cases`, `playbook_stats`.
- Đóng băng evidence/pre-state/post-state.
- Outcome evaluator 1h/24h/7d.
- UI cho operator đánh dấu verdict.

Điều kiện hoàn thành: mọi action mới sinh một case truy vết được đến Incident,
Action, Audit và telemetry.

### Pha 2 — Playbook Registry

- Chuẩn hóa contract cho action hiện có.
- Khai báo preflight, post-check, conflict scope và blast radius.
- Version playbook/command builder.
- Chặn tự động playbook thiếu contract.

Điều kiện hoàn thành: server có thể giải thích tất định vì sao action được hoặc
không được auto-run.

### Pha 3 — Trust Engine và Shadow Autopilot

- Tính statistics và Wilson lower bound.
- Tìm case tương tự.
- Ghi shadow decision, không execute.
- Dashboard so sánh shadow với operator/outcome.
- Chạy tối thiểu 2–4 tuần hoặc đủ sample.

Điều kiện hoàn thành: target/action precision đạt ngưỡng và không có unsafe
proposal trong cửa sổ đánh giá.

### Pha 4 — Autopilot L3 trên lab

- Chỉ mở 1–3 playbook SAFE blast radius thấp.
- Grace period, Telegram cancel, rate limit và cooldown.
- Per-cluster kill switch.
- Chaos/fault injection trên lab.

Điều kiện hoàn thành: các failure path, timeout, stale evidence, lock conflict và
service restart đều fail closed.

### Pha 5 — Canary production

- Bật từng playbook, từng cluster.
- Giới hạn một auto-action/ngày trong tuần đầu.
- Review hằng ngày mọi case.
- Tự hạ cấp khi metric xấu.
- Mở dần rate limit khi đạt SLO.

Điều kiện hoàn thành: success rate, false positive và rollback đạt gate trong ít
nhất 30 ngày.

### Pha 6 — L4 tự rollback

- Chuẩn hóa rollback contract.
- Verify rollback bằng telemetry.
- Circuit breaker theo playbook/fault family/cluster.
- Diễn tập rollback failure.

Điều kiện hoàn thành: rollback không làm tăng blast radius và mọi rollback failure
đều khóa tự động, chuyển operator.

### Pha 7 — L5 cho lỗi quen thuộc

- Bỏ grace period cho playbook đã được admin phê duyệt và cần phản ứng nhanh.
- Operator nhận báo cáo thay vì phê duyệt.
- Governance định kỳ và tự hạ cấp vẫn bắt buộc.
- Lỗi mới hoặc môi trường mới tự quay về L1/L2.

## 16. Nhóm playbook nên triển khai trước

Ưu tiên ứng viên có target rõ và post-check mạnh:

1. Diagnostic/read-only catalogue.
2. Restart đúng một daemon OSD bị `BLUESTORE_SLOW_OP` khi redundancy an toàn.
3. Restart service đơn lẻ đã dừng và có health check.
4. Metadata/config nhỏ, idempotent và có expected-state check.
5. Hành động dọn trạng thái tạm có ngưỡng dung lượng và tuổi rõ ràng.

Luôn giữ human approval hoặc block:

- Purge/zap/destroy OSD.
- Xóa pool, image, snapshot hoặc backup.
- Thay đổi CRUSH có rebalance lớn.
- Upgrade/downgrade/convert cluster.
- Reboot nhiều node hoặc node giữ quorum quan trọng.
- Thao tác không có rollback/post-check đáng tin cậy.

## 17. Kiểm thử bắt buộc

### Unit và property tests

- Classifier không thể hạ `DESTRUCTIVE` thành `SAFE`.
- Unknown action fail closed.
- Trust score không tăng từ case chưa verify.
- Duplicate case không làm tăng sample count.
- Version/scope khác nhau không dùng chung trust sai cách.
- Rate limit, cooldown và kill switch luôn thắng quyết định AI.

### Integration tests

- Incident → diagnosis → preflight → action → verify → case outcome.
- Restart Worker giữa execution/verification không chạy action lần hai.
- RabbitMQ redelivery giữ idempotency.
- Stale evidence bị chặn trước execution.
- Lock conflict chuyển pending/escalated đúng cách.
- Telegram cancel trong grace period ngăn execution.

### Fault injection

- SSH timeout/mất kết nối giữa command.
- Command exit 0 nhưng health code còn nguyên.
- Target biến mất hoặc đổi host.
- Telemetry/Loki unavailable.
- Database commit lỗi sau execution.
- Service crash trong rollback.
- Cluster bắt đầu recovery sau preflight.

### Security tests

- Prompt injection trong log không thể tạo command/action ngoài allowlist.
- Secret redaction trước LLM và case storage.
- Operator không đủ quyền không thể đổi autonomy level.
- Audit event không thể bị sửa qua API thông thường.

## 18. SLO và tiêu chí rollout

Theo dõi tối thiểu:

- `autonomous_remediation_verified_success_rate`.
- `autonomous_remediation_false_positive_rate`.
- `autonomous_remediation_rollback_rate`.
- `autonomous_remediation_mttv_seconds`.
- `autonomous_remediation_mttr_seconds`.
- `autopilot_preflight_block_total`.
- `autopilot_circuit_breaker_open_total`.
- `human_approval_override_rate`.
- `incident_recurrence_1h/24h/7d`.

SLO khởi đầu cho production L3:

- Verified success ≥ 98%.
- False positive ≤ 1%.
- Severe side effect = 0.
- Không có destructive auto-execution.
- 100% auto-action có preflight, audit và post-check.
- 100% case thất bại được hạ cấp/escalate, không retry vô hạn.

## 19. Definition of Done tổng thể

Hệ thống chỉ được coi là vận hành tự chủ khi:

1. Mỗi quyết định có evidence, policy result và audit đầy đủ.
2. AI không có đường chạy shell tự do.
3. Mọi auto-action đến từ playbook versioned có preflight/post-check.
4. Execution idempotent và chịu được restart/redelivery.
5. Thành công được xác minh bằng telemetry mới.
6. Outcome dài hạn cập nhật trust nhưng không tự sửa safety policy.
7. Playbook tự hạ cấp khi chất lượng suy giảm.
8. Operator có kill switch và can thiệp được trong giới hạn rõ ràng.
9. Lỗi mới mặc định quay về human-in-the-loop.
10. Tỷ lệ tự xử lý tăng bằng kết quả đo được, không bằng việc nới guardrail.

## 20. Nguyên tắc cuối cùng

AI được phép học **cách chọn đúng playbook trong hoàn cảnh đã được kiểm chứng**.
AI không được tự học **cách vượt qua policy hoặc tự tạo quyền mới**. Mức tự chủ
phải là kết quả của nhiều remediation đã verify, thống kê đủ mạnh và rollout
theo shadow → lab → canary → production, đồng thời luôn có đường tự hạ cấp khi
thực tế thay đổi.
