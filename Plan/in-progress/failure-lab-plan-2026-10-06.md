# Kế hoạch: Failure Lab — mô phỏng sự cố trên staging để AI có dữ liệu học

**Ngày lập:** 06/10/2026
**Trạng thái:** `IN_PROGRESS` — FL0 đang chờ operator xác nhận cấu hình vận hành; FL1 đang triển khai replay an toàn.
**Nguồn:** đề xuất "Failure Lab chỉ chạy trong staging" (06/10/2026), đối chiếu với code `main` + nhánh `sm-next` (`e849f52e`) và production ngày 06/10/2026.
**Liên quan:** `Plan/in-progress/self-monitoring-and-learning-loop-plan-2026-10-06.md` (LL0–LL7), `Plan/in-progress/autonomous-self-learning-operations-plan-2026-09-26.md`, `docs/lab-synthetic-incident.md`, `docs/lab-large-omap-training.md`.

---

## 1. Kết luận khả thi

**Làm được, và nên làm** — đây chính là lời giải cho nút thắt dữ liệu đã đo ở LL0:

- Vòng học hiện chỉ có **1** case `VERIFIED_SUCCESS` trên 4.953; 97 % incident là tín hiệu tự phát hiện mà đúng ra chỉ cần chẩn đoán, không có action an toàn để tự chạy. Chờ sự cố thật thì nhiều tháng cũng không đủ dữ liệu.
- Failure Lab tạo sự cố **có đáp án biết trước** (mã lỗi, nguyên nhân, playbook đúng, cách hồi phục), nên mỗi lượt chạy vừa là một case đã xác minh, vừa là một mẫu cho **golden set** (LL2) — hai thứ vòng học đang thiếu nhất.
- **CS-LAB là cụm staging** (operator xác nhận 06/10/2026), nên có sẵn môi trường thật để tạo lỗi, không cần dựng thêm cụm.

## 2. Những gì đã có trong repo

| Thành phần | Có gì | Còn thiếu |
|---|---|---|
| `shared/synthetic_incidents.py`, `scripts/lab/synthetic_incident.py`, trang `/synthetic-incidents` | 4 kịch bản giả lập (`osd_down`, clock skew, PG degraded, nearfull); chỉ nhận cụm `autonomy_environment=lab`; đánh dấu incident, Worker **chặn mọi thực thi**; không tính vào Trust production; dọn dẹp theo `run_id` | Chỉ có câu thông báo, **không có bằng chứng** (health detail, log, metric) → AI không có gì để chẩn đoán; không chấm điểm; **chưa từng được chạy** (0 incident giả lập trên production) |
| `scripts/lab/large_omap_training.sh` | Tạo lỗi **thật** LARGE_OMAP trên CS-LAB, khôi phục bằng `trap`, để Watcher/Worker tự sửa | Chỉ một kịch bản, viết bằng shell, không ghi kết quả có cấu trúc |
| Watcher → Worker → policy gate → verify → `RemediationCase` | Luồng đầy đủ; outcome xác minh bằng telemetry; `_is_lab_fault_injection` đã nhận diện case do harness tạo | Case lab không có nhãn đáp án nên không chấm được "chẩn đoán đúng hay sai" |
| `worker/ceph_capability_learning.py` | Biến finding đã xác minh thành đề xuất capability, qua cổng test/staging/review | — |
| `autonomy_environment` trên `Cluster` | Hiện **chỉ** mở khóa bộ giả lập; không đổi quyền tự sửa | CS-LAB đang là `production` → bộ giả lập từ chối chạy |

## 3. Nguyên tắc an toàn (bắt buộc)

1. **Chỉ chạy trên cụm được đánh dấu lab** (`autonomy_environment=lab`) **và** khớp `fsid` đã ghim trong cấu hình Failure Lab. Không xác nhận được cả hai thì không tạo lỗi.
2. **Kịch bản chỉ lấy từ danh mục đã duyệt** (file khai báo trong repo, qua review). AI không bao giờ tự nghĩ ra lệnh gây lỗi hay lệnh sửa; AI chỉ chọn `action_id` trong danh mục policy như hiện nay. Action RISKY/DESTRUCTIVE vẫn cần người duyệt, kể cả trên lab.
3. **Giới hạn phạm vi ảnh hưởng:** mỗi kịch bản khai báo trước tài nguyên được đụng (OSD nào, pool test nào, interface nào), thời hạn tối đa, và **luôn dọn dẹp** (kể cả khi lỗi hoặc bị ngắt), có kiểm tra sau dọn dẹp. Một lượt chạy tại một thời điểm (khóa toàn cục).
4. **Dừng khẩn cấp:** một công tắc tắt toàn bộ Failure Lab; cụm không về `HEALTH_OK` sau dọn dẹp thì dừng chiến dịch và báo Telegram.
5. **Không làm nhiễu số liệu production:** case lab mang nhãn nguồn `lab`; Trust Engine và autopilot của cụm production không dùng chúng để nâng quyền (giữ hành vi hiện có của bộ giả lập).
6. **Học = nhớ và tra cứu**, không phải tự sửa policy hay tự cấp quyền. Nâng quyền tự sửa cho một playbook chỉ qua tiêu chí ở FL5 **và** operator duyệt.

## 4. Thiết kế

### 4.1. Kịch bản (khai báo, review được)

`worker/policy/failure_lab_scenarios.yaml`, mỗi kịch bản:

- `id`, `fault_family` (mã lỗi mong đợi), `mode`: `replay` | `fault`;
- `preconditions`: cụm `HEALTH_OK`, đủ OSD up, pool test tồn tại…;
- `inject`: các bước cố định (command template trong executor đã có, không phải chuỗi tự do) — chỉ cho `mode: fault`;
- `replay`: health detail / log line / metric mẫu — cho `mode: replay`;
- `expect`: mã health/tín hiệu phải xuất hiện trong T giây, bằng chứng runbook phải thu được, **nguyên nhân đúng**, `action_id` chấp nhận được (hoặc "chỉ chẩn đoán");
- `recovery_check`, `timeout_seconds`, `cleanup` (luôn chạy), `blast_radius`.

### 4.2. Bộ chạy

Một máy trạng thái, mỗi bước ghi audit: kiểm tra môi trường → chụp trạng thái trước → tạo lỗi (hoặc phát lại dữ liệu) → chờ phát hiện → quan sát chẩn đoán và đề xuất → (action SAFE trên lab: theo quyết định 7.2; RISKY: chờ người duyệt) → kiểm tra hồi phục → dọn dẹp → chấm điểm → ghi kết quả.

### 4.3. Chấm điểm từng khâu

| Khâu | Tiêu chí |
|---|---|
| Phát hiện | Đúng mã lỗi trong T giây; không có incident lạ |
| Chẩn đoán | Nguyên nhân khớp đáp án; có trích bằng chứng runbook |
| Đề xuất | `action_id` nằm trong danh sách chấp nhận (hoặc đúng là chỉ chẩn đoán) |
| Hồi phục | Về trạng thái trước trong thời hạn; MTTR |
| Tác dụng phụ | Không có mã health mới ngoài dự kiến |
| Dọn dẹp | Cấu hình/tài nguyên về như cũ |

**Lượt đối chứng không tạo lỗi** chạy xen kẽ để đo báo động giả (AI tự bịa sự cố hoặc action).

### 4.4. Nối vào vòng học

- Mỗi lượt đạt → `RemediationCase` nguồn `lab` kèm **nhãn đáp án** (nguyên nhân, action đúng) → dùng cho tra cứu (LL1/LL3) và làm **golden set chẩn đoán** (LL2) chạy trong CI.
- Lượt chẩn đoán sai/đề xuất sai → ví dụ âm (LL6), không bao giờ dùng để cho phép hành động.
- Báo cáo chiến dịch trên `/ai-learning`: tỉ lệ đúng từng khâu theo kịch bản, xu hướng theo thời gian.

## 5. Gói việc

### FL0 — Chuẩn bị staging
- [ ] Đổi CS-LAB sang `autonomy_environment=lab` qua trang Cài đặt (có audit). Hiện chỉ ảnh hưởng bộ giả lập; ghi lại trong plan.
- [ ] Ghim `fsid` của CS-LAB trong cấu hình Failure Lab; bộ chạy từ chối mọi cụm khác.
- [ ] Pool/bucket test riêng (`test-*`) cho các kịch bản đụng dữ liệu.
- [ ] Kênh Telegram/nhãn riêng cho thông báo lab để không lẫn cảnh báo thật.

### FL1 — Replay có bằng chứng + chấm điểm (không đụng cụm)
- [ ] Mở rộng bộ giả lập: envelope có health detail, log line và metric mẫu lấy từ sự cố thật đã ghi (đã ẩn danh) thay vì một câu thông báo.
- [~] Danh mục YAML + validator cho 9 replay scenario đã có. Fixture hiện là dữ liệu tổng hợp, có provenance `synthetic_fixture`; chưa thay bằng log/metric sự cố thật đã ẩn danh nên chưa đủ điều kiện golden/learning.
- [~] Scorer cho detection/diagnosis/proposal/recovery/side effects/cleanup đã có; campaign JSON/CLI hỗ trợ chấm cả replay và no-fault control. Chưa có bộ tự chạy lượt đối chứng hay tự thu outcome từ Worker.
- [ ] Chạy trong CI như golden set chẩn đoán đầu tiên.

### FL2 — Lỗi thật trên CS-LAB
- [ ] Bộ chạy `mode: fault` với chốt chặn mục 3 (lab + fsid, khóa toàn cục, thời hạn, dọn dẹp luôn chạy, kiểm tra sau dọn dẹp).
- [ ] Kịch bản đầu, đều đảo ngược được: dừng một OSD daemon; trễ mạng có kiểm soát bằng `tc netem` trên một interface (luôn gỡ); hạ ngưỡng nearfull trên pool test; LARGE_OMAP (chuyển harness shell hiện có vào bộ chạy).
- [ ] Mỗi kịch bản chạy thử có người theo dõi trước khi cho chạy theo lịch.

### FL3 — Ghi kết quả thành tri thức
- [ ] `RemediationCase` nguồn `lab` + nhãn đáp án; tra cứu case dùng được (sau LL1); golden set lấy từ lượt lab đạt.

### FL4 — Chạy shadow theo lịch
- [ ] Chiến dịch định kỳ (giờ thấp điểm), báo cáo tỉ lệ từng khâu và báo động giả; dừng khi cụm không về `HEALTH_OK`.

### FL5 — Tiêu chí nâng quyền theo từng playbook
- [ ] Đề xuất tiêu chí: ≥ 10 lượt lab liên tiếp đạt hồi phục, 0 tác dụng phụ, rollback đã kiểm thử, chẩn đoán đúng ≥ 90 %. Đạt tiêu chí chỉ tạo **đề xuất** nâng quyền; operator duyệt mới có hiệu lực.

## 6. Rủi ro

| Rủi ro | Giảm thiểu |
|---|---|
| Tạo lỗi nhầm cụm | lab + fsid ghim; danh sách cụm hợp lệ chỉ có một |
| Lỗi không gỡ được | chỉ kịch bản đảo ngược được; `cleanup` luôn chạy; kiểm tra sau dọn dẹp; dừng chiến dịch khi không về `HEALTH_OK` |
| AI "học thuộc" kịch bản lab, kém với lỗi thật | replay dùng dữ liệu sự cố thật; đa dạng tham số (OSD/host ngẫu nhiên trong phạm vi); đo riêng trên sự cố thật |
| Nhiễu cảnh báo cho operator | kênh/nhãn lab riêng |
| Trộn số liệu lab vào quyết định production | nhãn nguồn; Trust/autopilot không dùng case lab |

## 7. Cần operator quyết định

1. Đổi CS-LAB sang `autonomy_environment=lab` (FL0).
2. Trên lab, action **SAFE** được tự chạy trong lượt Failure Lab hay vẫn chờ duyệt? (RISKY luôn chờ duyệt.)
3. Khung giờ chạy chiến dịch tự động (FL4) và kênh Telegram cho thông báo lab.
4. Danh sách kịch bản `mode: fault` được phép ở FL2.

## 8. Nhật ký

| Ngày | Hạng mục | Kết quả | Bằng chứng | Trạng thái |
|---|---|---|---|---|
| 06/10/2026 | Khảo sát | Bộ giả lập và harness LARGE_OMAP được rà soát; CS-LAB đang đánh dấu `production`, chưa đổi cấu hình | mục 2, 7 | Done |
| 06/10/2026 | FL1 replay scaffolding | Thêm catalog/validator 9 fixture, evidence envelope và scorer; fixture được đánh dấu synthetic, không eligible làm verified/golden case. `test_synthetic_incidents.py` + `test_incident_correlation.py` + `test_remediation_runbook.py`: 26 passed. | `worker/policy/failure_lab_scenarios.yaml`, `shared/synthetic_incidents.py` | In Progress |
| 06/10/2026 | FL1 review fixes | Scenario catalog không thể tự cấp golden approval; fixture vẫn bị fail-closed cho golden set đến khi có approval registry độc lập. Thêm scorer/report theo campaign JSON và CLI offline. 29 tests passed, Ruff và `git diff --check` passed; CLI smoke passed. Chưa tự thu thập outcome từ Ceph/Worker. | `shared/synthetic_incidents.py`, `scripts/lab/score_replay_report.py` | In Progress |
| 06/10/2026 | FL1 no-fault control scoring | Campaign report nhận run `kind=control` và tính false positive nếu incident, fault detection hoặc action xuất hiện. Đây chỉ là scorer offline; không chạy đối chứng trên cluster. 30 tests passed. | `shared/synthetic_incidents.py`, `tests/test_synthetic_incidents.py` | In Progress |
| 06/10/2026 | FL0 operator gates | Chưa đổi CS-LAB, chưa tạo pool/bucket, chưa cấu hình Telegram. Chờ operator xác nhận mục 7. | mục 7 | Blocked on operator decision |
