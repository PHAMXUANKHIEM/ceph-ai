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

### 4.5. Đánh giá đề xuất công cụ (07/10/2026)

Operator gửi một đề xuất công cụ bên ngoài; đánh giá và quyết định:

| Đề xuất | Quyết định | Lý do |
|---|---|---|
| `ceph/ceph` vstart.sh làm cụm lab | **Không dùng**; thay bằng VM cephadm một node (FL0) | Ý đúng: không gây lỗi trên cụm mà tool đang giám sát (CS-LAB là cụm mặc định duy nhất, autopilot bật, incident tính vào KPI học). Nhưng vstart cần build Ceph từ source (hàng giờ, hàng chục GB) và không có cephadm/orchestrator, nên `osd_down_fault` và đường `exec_mode=cephadm` của Watcher sẽ khác cụm thật. |
| `ceph/teuthology` | Chỉ tham khảo cách tổ chức setup/run/cleanup | Tích hợp toàn bộ quá nặng. |
| `chaos-mesh` | Không dùng | Chỉ cho Kubernetes/Rook; Ceph ở đây chạy cephadm trên máy thật. |
| `ceph-qa-suite` | Không dùng | Deprecated; test nằm trong `qa/` của repo chính. |
| `tc netem` | Giữ quyết định chưa cho phép | Chỉ trên mạng lab cách ly, có bước tự gỡ. |
| `promptfoo` | Làm sau (FL1), chạy theo lịch trên lab | Đã có scorer 6 khâu; promptfoo thêm so sánh nhiều prompt/model. Không chạy LLM trong GitHub CI. |
| `langfuse` | Hoãn | Trace cơ bản đã có (`shared/ai_observability.py`, `AutonomyDecision`, `RemediationCase`); Langfuse v3 tự host cần Postgres + ClickHouse + Redis + S3, quá nặng cho máy này; gửi ra ngoài thì phải redact hostname/IP. Chỉ đáng khi có hàng trăm lượt chạy cần duyệt nhãn. |
| GPU / fine-tune | Không cần | 2/8 là do thiếu bằng chứng và prompt chưa đòi nêu thực thể cụ thể, chưa phải do model. |

Thứ tự: deploy FL2 + bật evidence → chạy lại replay → VM cephadm lab → tiêu chí "từ chối khi thiếu bằng chứng" → promptfoo → (Langfuse nếu cần).

## 5. Gói việc

### FL0 — Chuẩn bị staging
- [x] CS-LAB đổi sang `autonomy_environment=lab` (06/10, operator yêu cầu; có bản ghi `AutopilotClusterConfigAudit`; autopilot giữ nguyên). Đã kiểm tra: trong Worker/Watcher/shared chỉ bộ giả lập đọc giá trị này.
- [x] Ghim `fsid`: `FAILURE_LAB_CLUSTER_FSID`; bộ chạy đọc `ceph fsid` thật và từ chối nếu khác (07/10). Operator cần đặt giá trị sau khi deploy.
- [ ] Pool/bucket test riêng (`test-*`) cho các kịch bản đụng dữ liệu.
- [ ] **Cụm lab dùng một lần** (mục 4.5): một VM `cephadm bootstrap --single-host-defaults`, 3 OSD trên loop device/LV; thêm làm cụm giám sát thứ 2 qua Deploy Cluster, đánh dấu `lab`, ghim fsid. Khi có cụm này, FL2 chạy trên nó; CS-LAB chỉ để giám sát. Cần operator cấp VM (~4 vCPU, 8 GB RAM, 3 × 20 GB).
- [x] Kênh Telegram riêng: `FAILURE_LAB_TELEGRAM_CHAT_ID` (bot incident, tiền tố `[FAILURE LAB]`); để trống = im lặng, không bao giờ rơi vào kênh incident thật (07/10). Operator cần cung cấp chat id.

### FL1 — Replay có bằng chứng + chấm điểm (không đụng cụm)
- [ ] Mở rộng bộ giả lập: envelope có health detail, log line và metric mẫu lấy từ sự cố thật đã ghi (đã ẩn danh) thay vì một câu thông báo.
- [~] Danh mục YAML + validator cho 9 replay scenario đã có. Fixture hiện là dữ liệu tổng hợp, có provenance `synthetic_fixture`; chưa thay bằng log/metric sự cố thật đã ẩn danh nên chưa đủ điều kiện golden/learning.
- [~] Scorer cho detection/diagnosis/proposal/recovery/side effects/cleanup đã có; campaign JSON/CLI hỗ trợ chấm cả replay và no-fault control. Bộ chạy chiến dịch `scripts/lab/failure_lab_replay.py` (`shared/failure_lab.py`) tự tạo incident giả lập, chờ Worker chẩn đoán, đọc chẩn đoán/đề xuất, chấm và dọn đúng `run_id`; báo cáo ở `/var/lib/ceph-ai/failure-lab/`. Còn thiếu: lượt đối chứng tự động (cần FL2/cụm thật) và chạy thật trên CS-LAB (cần FL0: `autonomy_environment=lab`).
- [ ] Chạy trong CI như golden set chẩn đoán đầu tiên — chỉ **chấm lại output đã ghi**, không gọi LLM trong GitHub CI (Worker gọi model qua CLI OAuth; đưa secret lên CI và tốn token mỗi lần push là không đáng).
- [ ] Tiêu chí **"từ chối khi thiếu bằng chứng"**: kịch bản không có bằng chứng thì chẩn đoán phải nói chưa chắc chắn thay vì đoán; thêm vào scorer như một khâu chấm riêng và vài kịch bản replay thiếu dữ liệu có chủ đích.
- [ ] (Sau) promptfoo chạy **theo lịch trên lab** để so sánh prompt/model trên cùng bộ kịch bản, qua provider `exec:`/python bọc router hiện có; tiêu chí chấm vẫn lấy từ `failure_lab_scenarios.yaml`.

### FL2 — Lỗi thật trên CS-LAB
- [x] Bộ chạy lỗi thật `shared/failure_lab_fault.py` + CLI `scripts/lab/failure_lab_fault.py`, danh mục riêng `worker/policy/failure_lab_faults.yaml` (chỉ các `kind` có trong code, không có lệnh tự do). Chốt chặn: `FAILURE_LAB_FAULT_ENABLED` (mặc định tắt), cụm `lab` + fsid ghim, file `HALT`, khóa `flock` toàn cục, không có incident thật cùng loại đang mở, mã health kỳ vọng chưa xuất hiện, lượt `--scheduled` chỉ trong `FAILURE_LAB_WINDOW`. Gỡ lỗi trong `finally` (cả khi Ctrl-C/SIGTERM, thử lại 3 lần), rồi chờ cụm về đúng tập mã health trước lượt chạy; không về được hoặc bị ngắt → ghi `HALT` + báo lab. Worker: incident phát hiện trong cửa sổ lượt chạy (+15 phút) → action SAFE chờ duyệt (`failure_lab_execution_held`), cảnh báo sang kênh lab. 15 test + 1 test Worker (07/10).
- [~] Kịch bản đầu (operator chốt 07/10): `osd_down_fault` — `ceph orch daemon stop/start` OSD up có id lớn nhất, chỉ khi `ceph osd ok-to-stop` đồng ý và `mon_osd_down_out_interval` ≥ thời hạn + 120 s (không để Ceph đánh dấu out và dời dữ liệu); `osd_nearfull_fault` — `ceph osd set-nearfull-ratio` xuống ngay dưới mức dùng của OSD đầy nhất rồi trả lại giá trị cũ. **Đính chính:** ngưỡng nearfull là của cả cụm, không có ngưỡng theo pool; trên lab nó chỉ sinh cảnh báo, không chặn ghi. Chưa làm: `tc netem` (chưa cho phép), LARGE_OMAP vào bộ chạy.
- [ ] Mỗi kịch bản chạy thử có người theo dõi trước khi cho chạy theo lịch.

### FL3 — Ghi kết quả thành tri thức
- [x] (09/10) Mỗi lượt lỗi thật ghi sự kiện `failure_lab_label` vào timeline của incident nó gây ra: nguyên nhân đã biết (vd "Failure Lab đã chủ động dừng daemon osd.5"), mã health kỳ vọng, `acceptable_action_ids`, điểm 6 khâu. Không cần migration.
- [x] (09/10) `shared/case_references.py` thêm loại tham khảo `lab_reproduced` lấy từ **mọi cụm** theo họ lỗi: chẩn đoán trên cụm production thấy nguyên nhân thật đã tái hiện trên lab, hành động chấp nhận và lần đó AI đúng hay sai. Chỉ là ngữ cảnh, không cấp quyền thực thi.
- [ ] Golden set chẩn đoán lấy từ lượt lab đạt (cần cụm staging chạy thật).

### FL4 — Chạy shadow theo lịch
- [ ] Chiến dịch định kỳ (giờ thấp điểm), báo cáo tỉ lệ từng khâu và báo động giả; dừng khi cụm không về `HEALTH_OK`.

### FL5 — Tiêu chí nâng quyền theo từng playbook
- [ ] Đề xuất tiêu chí: ≥ 10 lượt lab liên tiếp đạt hồi phục, 0 tác dụng phụ, rollback đã kiểm thử, chẩn đoán đúng ≥ 90 %. Đạt tiêu chí chỉ tạo **đề xuất** nâng quyền; operator duyệt mới có hiệu lực.

### FL6 — AI tự tái hiện lỗi thiếu bằng chứng trên staging (operator yêu cầu 09/10/2026)

Mục tiêu: lỗi production mà AI kết luận "chưa đủ bằng chứng" được tái hiện có kiểm soát trên cụm staging, để có nguyên nhân đã biết và rút ngắn việc học.

Luồng:
1. **Hàng chờ thiếu bằng chứng:** gom incident/case production có chẩn đoán thiếu bằng chứng hoặc độ tin thấp theo họ lỗi; ưu tiên họ lặp lại nhiều và chưa có tham khảo `lab_reproduced`.
2. **Nghiên cứu tài liệu:** code tải tài liệu Ceph chính thức cho mã health đó (docs.ceph.com health-checks, cùng nguồn đã trích); AI (CLI chỉ đọc, như các analyst nightly) đọc tài liệu + bằng chứng production và trả về **đề xuất tái hiện có cấu trúc**: kiểu lỗi (chỉ chọn trong bộ kiểu lỗi có trong code), tham số, mã health kỳ vọng, nguyên nhân, hành động chấp nhận, cách hồi phục, trích dẫn tài liệu.
3. **Kiểm tra tất định:** validator từ chối đề xuất có kiểu lỗi lạ, tham số ngoài giới hạn, thời hạn quá dài, hoặc nhắm cụm không phải staging (lab + fsid ghim). Nếu không kiểu lỗi nào phù hợp, AI chỉ được tạo **yêu cầu thêm kiểu lỗi mới** — thành việc viết code + review, không chạy được.
4. **Duyệt tạo lỗi trên Telegram:** thẻ nêu lỗi production gốc, đề xuất, tài liệu, phạm vi ảnh hưởng; nút "Cho phép tái hiện" / "Bỏ qua". Đề xuất được duyệt lưu thành kịch bản động (audit người duyệt).
5. **Chạy trên staging:** bộ chạy FL2 (khóa, HALT, gỡ lỗi trong `finally`, chờ hồi phục) với kịch bản đã duyệt.
6. **Duyệt sửa trên Telegram:** AI chẩn đoán và đề xuất `action_id` trong danh mục policy; operator cho phép thì thực thi trên staging (đường duyệt action hiện có, giữ trong cửa sổ lượt lab).
7. **Ghi học + đánh giá:** nhãn FL3; chấm 6 khâu; báo cáo Telegram: AI đúng ở khâu nào, sửa có hồi phục không; chỉ số theo họ lỗi: số lượt đến khi chẩn đoán đúng, thời gian tới chẩn đoán đúng, tỉ lệ đúng trước/sau khi có tham khảo lab.

Rào an toàn giữ nguyên mục 3: AI không viết lệnh shell; chỉ cụm staging; mọi lần tạo lỗi và mọi lần sửa đều qua người duyệt; HALT/khóa/gỡ lỗi luôn chạy; case lab không nâng quyền production.

Gói việc:
- [x] FL6.0 (= FL3) nhãn lab và tham khảo chéo cụm.
- [x] FL6.1 hàng chờ thiếu bằng chứng (`shared/evidence_gaps.py`, `python -m scripts.lab.evidence_gaps`).
- [x] FL6.2 tài liệu Ceph (`shared/ceph_docs.py`) + đề xuất AI có cấu trúc + validator (`shared/reproduction_proposal.py`, `scripts/lab/propose_reproduction.py`).
- [x] FL6.3 thẻ duyệt Telegram (`flrepro:ok/skip`, chỉ operator), quyết định ghi vào file đề xuất (`shared/reproduction_approval.py`).
- [x] FL6.4 `run_proposal`: đề xuất đã duyệt chạy một lần qua mọi cổng FL2, ghép với lỗi đã review cùng kiểu (cơ chế/gỡ lỗi từ catalog; đề xuất chỉ thêm mã kỳ vọng, đặt hành động đã duyệt, rút ngắn thời gian giữ lỗi); trạng thái RUNNING → DONE/FAILED; nhãn FL3 ghi `proposal_id`. CLI `--proposal <id>|next`. Duyệt sửa dùng đường giữ action + thẻ duyệt ở kênh lab của FL2.
- [ ] FL6.5 báo cáo học: chỉ số theo họ lỗi, Telegram sau mỗi lượt.
- [ ] Thêm kiểu lỗi mới theo yêu cầu (vd large omap trên pool test, OSD out, cờ `noout`), mỗi kiểu một PR có review.
- [x] Cấu hình cụm staging trên Dashboard (09/10): Cài đặt > Hệ thống > **Cụm Staging (Failure Lab)** chọn cụm (tự chuyển `lab`, cụm cũ về `production`, có audit), ghim fsid đọc từ cụm, khung giờ, chat Telegram lab, công tắc gây lỗi (chỉ bật được khi đã ghim fsid; đổi cụm thì tắt và bỏ fsid). Deploy Cluster có ô **Dùng cụm này làm cụm Staging** (khi đăng ký giám sát; chỉ khi chưa có cụm staging): xong deploy thì cụm mới là `lab`, fsid ghim từ MON, gây lỗi vẫn tắt. Lưu ở `/var/lib/ceph-ai/config/failure-lab.json` (`shared/failure_lab_config.py`), bộ chạy và Worker đọc mỗi lần dùng nên không cần restart; thiếu khóa thì dùng `FAILURE_LAB_*` trong .env.
- **Cần operator:** dựng cụm staging (hoặc deploy với ô Staging), rồi trong Cài đặt > Cụm Staging: ghim fsid, đặt chat Telegram lab, bật gây lỗi.

## 6. Rủi ro

| Rủi ro | Giảm thiểu |
|---|---|
| Tạo lỗi nhầm cụm | lab + fsid ghim; danh sách cụm hợp lệ chỉ có một |
| Lỗi không gỡ được | chỉ kịch bản đảo ngược được; `cleanup` luôn chạy; kiểm tra sau dọn dẹp; dừng chiến dịch khi không về `HEALTH_OK` |
| AI "học thuộc" kịch bản lab, kém với lỗi thật | replay dùng dữ liệu sự cố thật; đa dạng tham số (OSD/host ngẫu nhiên trong phạm vi); đo riêng trên sự cố thật |
| Nhiễu cảnh báo cho operator | kênh/nhãn lab riêng |
| Trộn số liệu lab vào quyết định production | nhãn nguồn; Trust/autopilot không dùng case lab |

## 7. Cần operator quyết định

Đã chốt 07/10/2026 (operator đồng ý đề xuất):

1. CS-LAB là `lab` — xong 06/10.
2. Action **SAFE vẫn chờ duyệt** trong lượt lab; cân nhắc cho tự chạy sau vài lượt FL2 có người theo dõi. RISKY luôn chờ duyệt.
3. Chạy theo lịch **02:00–05:00** (Asia/Ho_Chi_Minh); thông báo vào **kênh Telegram riêng** (chat id: chờ operator).
4. FL2 bắt đầu với **dừng 1 OSD** và **hạ ngưỡng nearfull**; chưa cho `tc netem`.

## 8. Nhật ký

| Ngày | Hạng mục | Kết quả | Bằng chứng | Trạng thái |
|---|---|---|---|---|
| 06/10/2026 | Khảo sát | Bộ giả lập và harness LARGE_OMAP được rà soát; CS-LAB đang đánh dấu `production`, chưa đổi cấu hình | mục 2, 7 | Done |
| 06/10/2026 | FL1 replay scaffolding | Thêm catalog/validator 9 fixture, evidence envelope và scorer; fixture được đánh dấu synthetic, không eligible làm verified/golden case. `test_synthetic_incidents.py` + `test_incident_correlation.py` + `test_remediation_runbook.py`: 26 passed. | `worker/policy/failure_lab_scenarios.yaml`, `shared/synthetic_incidents.py` | In Progress |
| 06/10/2026 | FL1 review fixes | Scenario catalog không thể tự cấp golden approval; fixture vẫn bị fail-closed cho golden set đến khi có approval registry độc lập. Thêm scorer/report theo campaign JSON và CLI offline. 29 tests passed, Ruff và `git diff --check` passed; CLI smoke passed. Chưa tự thu thập outcome từ Ceph/Worker. | `shared/synthetic_incidents.py`, `scripts/lab/score_replay_report.py` | In Progress |
| 06/10/2026 | FL1 no-fault control scoring | Campaign report nhận run `kind=control` và tính false positive nếu incident, fault detection hoặc action xuất hiện. Đây chỉ là scorer offline; không chạy đối chứng trên cluster. 30 tests passed. | `shared/synthetic_incidents.py`, `tests/test_synthetic_incidents.py` | In Progress |
| 06/10/2026 | FL1 review (phiên tiếp quản) | Sửa 5 điểm trước khi push: `load_scenario_catalog` độ phức tạp 30 → tách hàm nhỏ (≤ 10); danh mục **nạp lười** (`scenarios()`), vì Watcher/Worker import module này và một lỗi YAML từng có thể làm cả hai không khởi động được; lỗi mypy mới (stub `yaml`, kiểu đầu vào scorer); replay không bắt buộc hồi phục/dọn dẹp (không có gì để hồi phục); tiêu chí chẩn đoán nhận cách viết tiếng Việt (`down|dừng|ngừng`…) vì chẩn đoán thật viết tiếng Việt. 14 test synthetic + 159 test liên quan pass. | `shared/synthetic_incidents.py`, `worker/policy/failure_lab_scenarios.yaml` | In Progress |
| 06/10/2026 | Chiến dịch replay đầu tiên trên CS-LAB | **2/8 đạt** (bỏ `large_omap` vì có incident LARGE_OMAP thật đang mở). Đạt: clock skew, node unreachable. Chẩn đoán sai: `osd_down`, `osd_nearfull` — AI không nêu được OSD cụ thể (osd.0/osd.2) dù có trong log. Đề xuất khác kỳ vọng (`investigate_manually`): `restart_osd_daemon` cho osd_down/latency/pg_degraded/slow_heartbeat, `enable_pool_pg_autoscaler` cho crush_skew/nearfull — osd_down→restart có thể chấp nhận được, còn autoscaler cho nearfull/CRUSH skew là sai hướng. Cần operator duyệt lại `acceptable_action_ids` từng kịch bản. Sự cố vận hành: báo cáo không ghi được (thư mục thuộc root) → dựng lại từ DB; incident giả lập gửi cảnh báo Telegram thật → lượt này đã mute, đã sửa vĩnh viễn. | `/var/lib/ceph-ai/failure-lab/replay-first-20261006.json` | Done |
| 06/10/2026 | Duyệt tiêu chí đề xuất | Operator duyệt: `osd_down` chấp nhận `restart_osd_daemon`; autoscaler cho nearfull/CRUSH skew và restart OSD cho PG degraded/slow heartbeat là sai (giữ chỉ điều tra). Chấm lại chiến dịch đầu: vẫn 2/8 — osd_down giờ chỉ trượt ở chẩn đoán (không nêu osd.0). Việc tiếp theo cho vòng học: chẩn đoán phải nêu thực thể cụ thể từ log; đề xuất không dùng autoscaler/restart cho mã lỗi không liên quan. | `worker/policy/failure_lab_scenarios.yaml` | Done |
| 06/10/2026 | FL0 operator gates | Chưa đổi CS-LAB, chưa tạo pool/bucket, chưa cấu hình Telegram. Chờ operator xác nhận mục 7. | mục 7 | Blocked on operator decision |
| 07/10/2026 | Evidence không đụng incident giả lập | Bước thu bằng chứng (WP3.3/3.4) chưa từng chạy vì `INVESTIGATION_ENABLED` mặc định tắt. Trước khi bật cho CS-LAB: Worker gate và scanner Watcher bỏ qua incident có `synthetic_injection` (bằng chứng của cụm thật đang khỏe sẽ mâu thuẫn kịch bản và làm hỏng điểm replay); mọi lần bỏ qua đều ghi log lý do; tab Luồng AI hiện TẮT khi cờ tắt. Bằng chứng giả lập giống thật vẫn là việc FL1 còn lại. | `worker/llm/evidence_gate.py`, `watcher/investigation_scanner.py`, `shared/ai_flow.py` | Done |
| 07/10/2026 | FL2 bộ chạy lỗi thật | Xem mục FL2. Chưa chạy trên cụm: cần deploy, đặt `FAILURE_LAB_CLUSTER_FSID`, `FAILURE_LAB_FAULT_ENABLED=true`, rồi chạy thử từng kịch bản có người theo dõi. | `shared/failure_lab_fault.py`, `worker/policy/failure_lab_faults.yaml` | In Progress |
| 09/10/2026 | FL3 nhãn lab + FL6 thiết kế | Lượt lỗi thật ghi `failure_lab_label`; tham khảo `lab_reproduced` chéo cụm trong prompt chẩn đoán; thêm FL6 (AI tái hiện lỗi thiếu bằng chứng, hai lần duyệt Telegram). 29 test failure lab + case references pass. | `shared/failure_lab_fault.py`, `shared/case_references.py` | In Progress |
| 07/10/2026 | Đánh giá đề xuất công cụ | vstart → thay bằng VM cephadm một node; teuthology chỉ tham khảo; chaos-mesh/ceph-qa-suite không dùng; promptfoo làm sau, chạy theo lịch trên lab; Langfuse hoãn. Thêm việc: cụm lab dùng một lần (FL0), tiêu chí "từ chối khi thiếu bằng chứng" và promptfoo (FL1). | mục 4.5 | Done |
