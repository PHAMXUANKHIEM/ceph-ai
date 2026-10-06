# Kế hoạch: giám sát chính Ceph AI và mở nút thắt vòng tự học

**Ngày lập:** 06/10/2026
**Trạng thái:** `PLANNED` — chưa bắt đầu; chờ operator chốt các quyết định ở mục 6.
**Nguồn:** báo cáo "Nâng cấp cơ chế tự học của Ceph AIOps" (06/10/2026) và nhận xét "không có systemd, chỉ chạy bằng nohup", đối chiếu với code `main` (`3f081af7`) và production CS-LAB ngày 06/10/2026.
**Liên quan:** `Plan/incompleted/autonomous-self-learning-operations-plan-2026-09-26.md` (roadmap 4.3), `docs/immutable-production-release.md`, `docs/operations/reliability-slo.md`.

Hai phần độc lập, làm phần A trước: một hệ thống giám sát chết im lặng nguy hiểm hơn một AI học chậm.

---

## 0. Nguyên tắc

- **Đo trước, sửa sau.** Mỗi gói việc có chỉ số và bằng chứng trên production; không ghi "Done" khi chỉ có test.
- **Không chạy lệnh nguy hiểm để thử:** khởi động lại máy, dừng RabbitMQ hay container chỉ làm trong cửa sổ bảo trì operator đã duyệt.
- **Kênh cảnh báo "tôi đã chết" không được đi qua chính thứ đã chết:** không dùng `telegram-ai`, outbox hay RabbitMQ của stack để báo stack chết.
- **Không gửi dữ liệu cụm ra ngoài** khi chưa có quyết định của operator (mục 6).
- Những gì học được vẫn chỉ là ngữ cảnh hoặc khuyến nghị; quyền chạy lệnh vẫn do policy gate quyết định.

## 1. Hiện trạng đã kiểm chứng (06/10/2026)

### 1.1. Vận hành và tự giám sát

| Nhận định | Thực tế | Bằng chứng |
|---|---|---|
| "Không có systemd, chỉ nohup" | **Đã lỗi thời.** Stack chạy bằng `ceph-ai-containers.service` (enabled, gọi `container-up` lúc boot), 6 timer, unit thu log, code-repair supervisor. Container có `restart=unless-stopped` và healthcheck. Các unit chạy trần cũ (`ceph-ai-watcher/worker/dashboard.service`) đã disabled | `systemctl list-unit-files`, `podman inspect` |
| README | **Sai so với thực tế:** mục 8 vẫn hướng dẫn `nohup ... & disown` và ghi "Repo không dùng systemd unit" (dòng 261–304). Đây là nguồn của nhận định trên | `README.md` |
| RabbitMQ sau khi khởi động lại máy | **Sẽ không tự lên.** Container `rabbitmq` tạo tay ngày 28/07, `restart policy` rỗng, không nằm trong danh sách service của `container-up`; `podman-restart.service` disabled. Máy chạy liên tục từ 27/07 nên chưa ai thấy | `podman inspect rabbitmq`, `container-up:39` |
| Cảnh báo khi watcher/worker chết | **Chỉ tính khi có người mở trang.** `shared/reliability.collect_reliability()` có cảnh báo `service_heartbeat_stale` (critical) nhưng chỉ được gọi từ trang System Health và script soak; không có gì đẩy cảnh báo ra Telegram | `dashboard/routes/system_health.py:25` |
| Container unhealthy | **Không ai xử lý:** `HealthcheckOnFailureAction=none`; container hỏng mà không thoát thì vẫn chạy ở trạng thái unhealthy. `vault-monitor` không có healthcheck | `podman inspect` |
| Máy chết hoặc mất mạng | **Không ai biết:** không có tín hiệu nào từ bên ngoài máy (dead-man switch) | — |
| Đã thử khởi động lại máy chưa | **Chưa có bằng chứng** sau khi chuyển sang container (uptime từ 27/07/2026) | `uptime -s` |
| Tiến trình chạy tay còn sót | Các uvicorn thử nghiệm cũ (cổng 8099 ×2 từ 10 ngày, 18001 từ 14 ngày) và một vòng `bash` chờ pytest từ 19 ngày | `ps` |

Database production là PostgreSQL ở máy khác (không bị ảnh hưởng khi máy này khởi động lại, nhưng cũng cần được kiểm tra kết nối).

### 1.2. Vòng tự học

| Nhận định của báo cáo | Kết quả kiểm tra |
|---|---|
| Case retrieval bắt trùng tập node | Đúng (`shared/case_retrieval.py:82`), nhưng còn chặt hơn: xem dòng `fault_family` bên dưới |
| Case thất bại bị loại | Đúng (`_BAD_VERDICTS`) |
| RAG 4 file, điểm không có IDF, tài liệu Ceph chưa nạp | Đúng (`shared/natural_language/retrieval.py:306, 332–335`) |
| Template log regex dễ vỡ | Đúng; 191 case `LOG_ANOMALY` sinh **181** biến thể |
| Không có golden set | Đúng một nửa: có 100 câu Chat (`tests/fixtures/nl_queries_vi.yaml`), **không có** cho chẩn đoán incident |
| sqlite-vec trong "SQLite đang dùng" | Sai: production là **PostgreSQL**, extension **pgvector có sẵn** để bật |

Những điều báo cáo bỏ sót (quan trọng hơn):

- **A. Vòng học không có dữ liệu.** 4.953 `RemediationCase`: 3.872 `PROPOSED` (đều `PENDING_APPROVAL`, từ 04/09), 1.064 `REJECTED` (phần lớn `NODE_UNREACHABLE`), 14 `EXECUTION_FAILED`, **1 `VERIFIED_SUCCESS`**; 3 verdict của operator. Case Memory, Trust Engine, bandit/OPE đều chỉ học từ case đã xác minh.
- **B. `fault_family` chứa thực thể** (`OSD_LATENCY_HIGH:6`, `NODE_UNREACHABLE:10.3.53.69`): 11 mã lỗi thành 232 giá trị; lọc cứng `fault_family ==` chạy trước bước so node, nên nới tập node cũng không giúp `osd.0` cho `osd.6`.
- **C. RAG không tham gia chẩn đoán incident:** `KnowledgeStore` chỉ dùng trong Chat (`dashboard/chat_client.py`); worker chỉ dùng `find_verified_cases`.
- **D. `evidence_fingerprint` đã có ở mọi case** nhưng là SHA-256 của toàn bộ đầu vào: chỉ so trùng tuyệt đối, không tính được độ giống.

---

## 2. Phần A — giám sát chính hệ thống giám sát

### SM1 — Khởi động lại máy phải tự lên đủ
- [ ] Đưa RabbitMQ vào vòng đời của stack: service `rabbitmq` trong `compose.yaml` (giữ volume dữ liệu và user hiện có) hoặc ít nhất `container-up`/`container-down` khởi động nó trước `watcher`/`worker`; restart policy `always`. Không tạo lại container trước khi sao lưu định nghĩa queue/user (`rabbitmqctl export_definitions`).
- [ ] `container-up` chờ RabbitMQ và DB sẵn sàng (có timeout) trước khi bật service phụ thuộc; log rõ lý do nếu không lên.
- [ ] Bật `podman-restart.service` hoặc xác nhận `ceph-ai-containers.service` đủ thay thế; ghi lại lựa chọn.
- [ ] Test tĩnh: mọi container mà stack cần đều được `container-up` bật (kiểm tra danh sách so với compose + RabbitMQ).

### SM2 — Tự chữa khi container hỏng mà không thoát
- [ ] `HealthcheckOnFailureAction=restart` (podman 4.9 hỗ trợ `--health-on-failure`) cho service có healthcheck; giới hạn số lần restart để không lặp vô hạn.
- [ ] Thêm healthcheck cho `vault-monitor`.
- [ ] Ghi sự kiện "container tự restart vì unhealthy" để phần SM3 báo ra ngoài.

### SM3 — Cảnh báo đẩy, không phụ thuộc stack
- [ ] `ceph-ai-selfcheck.timer` (systemd, mỗi 1 phút, ngoài container) chạy một script độc lập: container nào thiếu/unhealthy, heartbeat trong `/run/ceph-ai` cũ, RabbitMQ không trả lời, DB không kết nối được, outbox Telegram tồn đọng, đĩa đầy.
- [ ] Gửi Telegram **trực tiếp** (curl tới Bot API bằng token đọc từ `.env`, không qua `telegram-ai`/outbox/RabbitMQ); chống spam: chỉ báo khi đổi trạng thái, nhắc lại theo chu kỳ, báo "đã hồi phục".
- [ ] Đưa `service_heartbeat_stale` và các cảnh báo critical của `collect_reliability()` vào cùng kênh (hiện chỉ hiện khi mở trang).
- [ ] Thông báo lúc khởi động: "Ceph AI vừa khởi động lại lúc … · service đã lên: …" để mọi lần reboot đều có dấu vết.

### SM4 — Tín hiệu từ bên ngoài máy (dead-man switch)
Cảnh báo trong máy không thể báo khi chính máy chết. Cần một bên ngoài chờ "nhịp tim" và báo khi mất:
- [ ] Endpoint nhẹ `/healthz` (đã có hoặc thêm) trả tổng trạng thái, không lộ chi tiết nội bộ.
- [ ] Bên kiểm tra theo lựa chọn ở mục 6 (máy khác trong mạng nội bộ chạy cron curl + Telegram, hoặc dịch vụ dead-man bên ngoài nhận ping đi ra). Cảnh báo khi quá 5 phút không có nhịp tim.

### SM5 — Diễn tập và tài liệu
- [ ] Diễn tập khởi động lại máy trong cửa sổ bảo trì: đo thời gian từ boot tới khi mọi service healthy, xác nhận nhận được thông báo SM3 và cảnh báo SM4 lúc máy tắt. Lưu biên bản vào `docs/operations/`.
- [ ] Diễn tập lỗi đơn lẻ (dừng RabbitMQ, kill watcher, làm DB không kết nối được) trên lab: mỗi lỗi phải có cảnh báo trong 2 phút và tự hồi phục nếu thuộc SM2.
- [ ] Sửa README mục 8: tách "chạy thử local (dev)" khỏi "production (systemd + container)", bỏ câu "Repo không dùng systemd unit".
- [ ] Dọn tiến trình chạy tay còn sót (cần operator xác nhận từng cái không còn dùng).

**Chỉ số hoàn thành phần A:** reboot drill thành công; mỗi lỗi đơn lẻ có cảnh báo Telegram ≤ 2 phút; máy tắt có cảnh báo từ bên ngoài ≤ 5 phút; 0 container chạy unhealthy quá 5 phút mà không có cảnh báo.

---

## 3. Phần B — mở nút thắt vòng tự học

Thứ tự theo nút thắt thật, không theo thứ tự công cụ của báo cáo.

### LL0 — Cho vòng học có dữ liệu (điều kiện của mọi bước sau)
- [ ] Phân tích 3.872 case `PROPOSED`: bao nhiêu thuộc incident đã tự hết, bao nhiêu là đề xuất lặp của cùng một incident dao động, bao nhiêu thật sự chờ người duyệt. Đóng case mồ côi bằng outcome mới `EXPIRED` (không tính là thành công hay thất bại).
- [ ] Gom đề xuất lặp: một incident đang mở chỉ giữ một case đề xuất cho mỗi playbook.
- [ ] 1.064 case `NODE_UNREACHABLE` bị `REJECTED`: xác định ai/cái gì từ chối (preflight, operator, auto) và có đúng không.
- [ ] Action SAFE đã chạy: tự xác minh kết quả theo hậu kiểm (post-state) để có `VERIFIED_SUCCESS`/`INEFFECTIVE` mà không cần chờ người.
- [ ] Chỉ số trên `/ai-learning`: số case đã xác minh mỗi tuần, tỉ lệ đề xuất được xử lý, số verdict. Mục tiêu đặt sau khi có số tuần đầu.

### LL1 — Tách mã lỗi khỏi thực thể
- [ ] `fault_family` chỉ còn mã lỗi (`OSD_LATENCY_HIGH`); thực thể (`osd.6`, IP node) sang cột riêng. Migration chuyển dữ liệu cũ, giữ nguyên giá trị gốc để tra cứu.
- [ ] Case retrieval: lọc cứng theo mã lỗi, bản Ceph chính, outcome đã xác minh; thực thể, tập node và kiểu deploy thành điểm xếp hạng.

### LL2 — Golden set cho chẩn đoán
- [ ] 50–100 incident có đáp án (root cause, playbook đúng, tài liệu liên quan) từ lab synthetic (`docs/lab-synthetic-incident.md`), 3 verdict `CORRECT` và incident đã đóng có kết luận rõ. Chỉ số: RCA đúng trong top-3, recall@3 của retrieval. Chạy trong CI cạnh `nl_queries_vi.yaml`.

### LL3 — Fingerprint đo được độ giống
- [ ] Lưu thêm tập health code và tập template log của mỗi case (bên cạnh hash hiện có); điểm Jaccard trong xếp hạng LL1.

### LL4 — Nối tri thức vào chẩn đoán incident
- [ ] Worker dùng `KnowledgeStore` khi chẩn đoán (hiện chỉ Chat dùng).
- [ ] Nạp trang health checks chính thức theo từng bản (thêm Nautilus nếu còn cụm Nautilus) và `prometheus_alerts.yml` của ceph-mixin, mỗi mã một chunk; thay công thức điểm bằng BM25 (`bm25s`); giữ lọc phiên bản và chặn secret.

### LL5 — Log template học được
- [ ] Drain3 chạy shadow cạnh regex hiện tại (sau bước redact), so số template và độ ổn định fingerprint trước khi thay.

### LL6 — Học từ thất bại
- [ ] Case `INEFFECTIVE`/regress vào prompt dưới mục "đã thử, không hiệu quả", không bao giờ dùng để cho phép hành động. Làm khi LL0 sinh đủ dữ liệu.

### LL7 — Sau khi golden set chứng minh có lợi
- [ ] Embedding đa ngôn ngữ (BGE-M3) + reranker với **pgvector**; đo RAM/CPU trên máy hiện tại trước (mô hình khoảng 2 GB, máy chỉ có CPU). Chỉ bật khi recall@3 tăng so với BM25.
- [ ] DSPy chọn few-shot, Ragas đo bám nguồn trong CI, học chéo cụm cho tri thức (quyền thực thi vẫn tính riêng từng cụm).

Không dùng LangChain, LlamaIndex, GraphRAG.

---

## 4. Thứ tự và phụ thuộc

1. **SM1 → SM3 → SM2 → SM4 → SM5** (rủi ro mất giám sát cao nhất, sửa ít code).
2. **LL0** song song với SM3 (chỉ đọc và đóng case, không đụng hạ tầng).
3. **LL1 → LL2 → LL3 → LL4 → LL5 → LL6 → LL7**; từ LL3 trở đi chỉ merge khi chỉ số golden set không giảm.

## 5. Rủi ro

| Rủi ro | Giảm thiểu |
|---|---|
| Đưa RabbitMQ vào compose làm mất queue/user | Export definitions trước; làm trong cửa sổ bảo trì; có bước rollback về container cũ |
| Restart khi unhealthy gây vòng lặp restart | Giới hạn số lần, cảnh báo khi vượt |
| Selfcheck báo động giả, gây nhờn cảnh báo | Chỉ báo khi đổi trạng thái; ngưỡng có xác nhận 2 lần liên tiếp |
| Đóng case `EXPIRED` làm mất dữ liệu học | Không xóa; outcome mới, có thể mở lại; không tính vào Trust Engine |
| Migration `fault_family` làm vỡ truy vấn cũ | Giữ cột gốc; test truy vấn dashboard/Trust Engine trước khi đổi |

## 6. Cần operator quyết định

1. **Dead-man switch bên ngoài (SM4):** chạy kiểm tra trên một máy nội bộ khác (cần chỉ định máy), hay dùng dịch vụ bên ngoài nhận ping (máy này gửi ra Internet một ping không chứa dữ liệu cụm)?
2. **Cửa sổ bảo trì** cho đưa RabbitMQ vào stack và diễn tập khởi động lại máy.
3. **Case `PROPOSED` mồ côi:** đồng ý đóng bằng `EXPIRED` sau bao lâu (đề xuất: incident đã resolved, hoặc đề xuất quá 7 ngày)?
4. **Tiến trình chạy tay còn sót:** cho phép dừng các uvicorn thử nghiệm cổng 8099, 18001 và vòng chờ pytest 19 ngày.

## 7. Nhật ký

| Ngày | Hạng mục | Kết quả | Bằng chứng | Trạng thái |
|---|---|---|---|---|
| 06/10/2026 | Khảo sát | Xác nhận 3 lỗ hổng tự giám sát (RabbitMQ không tự lên, cảnh báo chỉ khi mở trang, không có tín hiệu ngoài máy) và nút thắt dữ liệu của vòng học (1/4.953 case đã xác minh) | mục 1 | Done |
