# Vitastor AI — Roadmap và nhật ký bàn giao

Tài liệu này là nguồn theo dõi chung cho các tính năng AI của Vitastor trong `ceph-ai`.
Mỗi pull request/commit phải cập nhật checkbox, mục **Nhật ký triển khai**, kiểm thử và
commit gần nhất. Không đánh dấu hoàn thành nếu tiêu chí nghiệm thu chưa đạt.

## Quy ước trạng thái

- `[ ]` Chưa làm
- `[~]` Đang làm hoặc mới hoàn thành một phần
- `[x]` Hoàn thành và đã có kiểm thử
- Mọi thao tác thay đổi cluster phải có preview, phân loại rủi ro và phê duyệt.
- AI không được tuyên bố đã chạy lệnh nếu hệ thống không có bằng chứng thực thi.
- Evidence gửi tới AI không chứa SSH key, API key, token hoặc mật khẩu.

## Danh sách tính năng

- [x] **1. AI chẩn đoán nguyên nhân sự cố — bản lõi hoàn thành**
  - [x] Thu thập evidence read-only: status, pool, OSD, image và lỗi từng nguồn.
  - [x] Lưu diagnostic run bền vững với trạng thái `RUNNING/COMPLETED/FAILED` và evidence.
  - [x] Sinh chẩn đoán có nguyên nhân, ảnh hưởng, độ tin cậy, bằng chứng và bước xử lý.
  - [x] Hiển thị chẩn đoán trên Dashboard, không tự thực thi lệnh.
  - [x] Dùng chung output contract cho Router API, Codex và Claude.
  - [x] Kiểm thử persistence, lỗi AI, JSON xấu và không rò rỉ credential.
  - [ ] Mở rộng sau: PG chi tiết, correlation metric và lifecycle incident tự động.
- [x] **2. Baseline động và phát hiện bất thường** theo cluster/pool/OSD/volume/khung giờ.
  - [x] Chuẩn hoá metric cho bốn loại entity, không đưa credential vào sample.
  - [x] Median/MAD robust, ngưỡng tương đối và ngưỡng sàn chống nhiễu/outlier.
  - [x] Ưu tiên baseline cùng khung giờ khi đủ mẫu, fallback cửa sổ gần nhất.
  - [x] Không coi lần tải đầu tiên sau baseline idle là IOPS/bandwidth anomaly.
  - [x] Lifecycle `OPEN/RESOLVED`, persistence, Telegram transition và Dashboard API/UI.
  - [x] Retention dùng chung chính sách metric Vitastor.
- [ ] **3. Dự báo đầy dung lượng** và thời gian tới các mốc 80/90/95%.
  - [ ] Hồi quy robust (Theil–Sen) trên time-series 30 ngày đã có; cảnh báo theo pool
        và theo OSD lệch nhất, không chỉ theo tổng dung lượng cụm.
- [~] **4. Vòng lặp khắc phục đóng (closed-loop remediation)** — nền tảng cho bảo trì OSD.
  - [x] Model `VitastorRemediationAction` + `VitastorAuditEntry`, migration riêng, cô lập khỏi Ceph.
  - [x] Policy SAFE/RISKY bảo thủ mặc định (AD-5) + `action_id` đóng, không có shell tự do.
  - [x] Command builder đóng (start/restart OSD·mon·etcd, resync_time) + allowlist host khi thực thi.
  - [x] Proposer tất định từ telemetry: OSD `up:false` → đề xuất `restart_osd_service` (chờ duyệt).
  - [x] Watcher tự sinh đề xuất (dedup), auto-run SAFE, cảnh báo Telegram khi có RISKY chờ duyệt.
  - [x] Dashboard: thẻ Khắc phục (Duyệt/Từ chối) + Nhật ký hành động; API approve/reject/audit gated theo Vitastor admin.
  - [ ] **4.1 Xác minh sau khắc phục** — sau khi chạy lệnh, poll lại telemetry và chỉ đóng
        action khi tín hiệu đã hết (OSD `up:true` trở lại); ngược lại `FAILED` + báo Telegram.
        Tương đương `watcher/verify.py` của Ceph.
  - [ ] **4.2 Tự huỷ đề xuất khi tín hiệu đã hết** — action `PENDING_APPROVAL` chuyển
        `OBSOLETE` khi OSD tự up lại, không bắt operator từ chối thủ công.
  - [ ] **4.3 Duyệt bằng nút bấm trên Telegram** — mở rộng `dashboard/telegram_approval_bot.py`
        quét thêm bảng `vitastor_remediation_actions`.
  - [ ] **4.4 Sinh đề xuất từ Diagnosis và Anomaly** — thêm `source=DIAGNOSIS` / `source=ANOMALY`
        bên cạnh proposer tất định từ `status`, để mục 1–2 thực sự nối được vào vòng lặp khắc phục.
  - [ ] **4.5 Policy dạng file thay vì hard-code** — chuyển `SAFE_ACTION_IDS` sang YAML kiểu
        `worker/policy/action_policy.yaml`; đổi SAFE/RISKY là quyết định vận hành, không phải sửa code.
  - [ ] **4.6 dry-run / reweight OSD** và theo dõi tiến độ rebalance.
  - Xem thiết kế chi tiết: `docs/vitastor-remediation.md`.
- [ ] **5. Trợ lý tối ưu pool và PG** với mô phỏng tác động trước thay đổi.
  - [ ] Mô phỏng trước khi đổi `pg_size`/`pg_count`/`failure_domain`: bao nhiêu dữ liệu phải
        di chuyển, ước lượng thời gian, ảnh hưởng tới client I/O.
  - [ ] Dùng `create-pool` / `modify-pool` của `vitastor-cli`, luôn qua preview + phê duyệt.
- [ ] **6. Quản lý scrub thông minh** theo tải và phát hiện inconsistent/corrupted object.
  - [ ] Điều khiển `auto_scrub`, `scrub_interval`, `scrub_queue_depth` ở cấp pool/OSD.
  - [ ] Chọn cửa sổ tải thấp từ baseline theo khung giờ đã có ở mục 2.
- [ ] **7. Phân tích object lỗi** bằng `describe`; `fix` luôn là thao tác rủi ro cao.
  - [ ] `describe --inconsistent` làm evidence cho AI; `fix` bắt buộc hai bước xác nhận, không bao giờ auto.
- [ ] **8. AI quản lý volume/snapshot**: stale volume, retention, flatten/merge dependency.
  - [ ] Phát hiện image không có client chạm trong N ngày, chuỗi snapshot quá dài cần `flatten`/`merge`,
        image mồ côi sau khi VM bị xoá.
- [ ] **9. Prometheus và biểu đồ lịch sử nâng cao**: percentile, correlation và export.
  - [ ] Scrape trực tiếp endpoint metrics của `vitastor-mon` thay vì chỉ parse `vitastor-cli --json`
        theo chu kỳ — cho percentile latency và độ phân giải tốt hơn nhiều.
- [ ] **10. Incident timeline và AI postmortem** có audit đầy đủ.
  - [ ] Gom `VitastorAnomalyEvent` + `VitastorDiagnosticRun` + `VitastorRemediationAction` +
        `VitastorAuditEntry` (hiện là bốn bảng rời) vào một `VitastorIncident` duy nhất.
  - [ ] Dòng thời gian "phát hiện → chẩn đoán → đề xuất → duyệt → thực thi → xác minh",
        AI viết postmortem từ chính timeline đó.
- [ ] **11. AI capacity planner** từ mục tiêu VM/workload/failure domain.
- [ ] **12. Safe Autopilot** với ba cấp tự động, một lần duyệt và hai bước xác nhận.
  - Chỉ triển khai **sau khi** 4.1 (xác minh) và 10 (timeline) hoàn thành — không có bằng chứng
    thực thi và đường lùi thì tự động hoá là rủi ro thuần.
- [ ] **13. Log Intelligence / RCA cho Vitastor** — khoảng cách lớn nhất so với bản Ceph.
  - [ ] Hiện `vitastor/client.py::query_logs` mới chỉ `journalctl` qua SSH, không phân tích.
  - [ ] Tái dùng `watcher/log_source/{loki,ssh_tail}.py`, `watcher/log_analysis.py`, `watcher/log_intel.py`.
  - [ ] Viết `vitastor/log_families.py` thay cho `watcher/ceph_code_families.py`: `slow op`,
        journal/metadata đầy, etcd lease lost, PG peering stuck, disk I/O error.
- [ ] **14. Vòng đời ổ đĩa và OSD qua `vitastor-disk`** — thao tác vận hành thiếu đáng giá nhất hiện nay.
  - [ ] `prepare` / `start` / `purge` / `resize` / `raw-resize` / `trim` / `upgrade-simple`.
  - [ ] Kịch bản có hướng dẫn từng bước: thêm OSD mới, thay ổ hỏng, rút OSD an toàn
        (drain → `rm-osd`), mở rộng dung lượng. Mỗi bước có preview + phê duyệt như `operations.py`.
- [ ] **15. Phát hiện config drift và version skew** — nguyên nhân "chậm bí ẩn" đặc thù Vitastor.
  - [ ] So sánh chéo toàn cụm: `immediate_commit` lệch giữa các OSD, journal/metadata không nằm
        trên NVMe ở một số node, `/etc/vitastor/vitastor.conf` khác nhau, version OSD/mon/etcd không đồng nhất.
- [ ] **16. Kiểm tra an toàn cấu hình EC pool**
  - [ ] Cảnh báo `pg_minsize` đặt sai, `failure_domain` khiến mất một node là mất dữ liệu,
        số OSD không đủ cho `pg_size`.
- [ ] **17. Etcd operations** — hiện mới *giám sát* latency/quorum/db size, chưa có hành động.
  - [ ] Backup snapshot etcd định kỳ; compaction/defrag khi db phình; restore từ snapshot (RISKY).
- [ ] **18. Bản đồ client đang dùng image** — QEMU/VDUSE/NBD/ublk/CSI/NFS.
  - [ ] Chặn `rm` image đang được mount; map PVC ↔ image cho cụm K8s dùng CSI.
- [ ] **19. Benchmark tích hợp** — theo `docs/usage/fio.en.md` của upstream.
  - [ ] Chạy fio chuẩn hoá trên image test, lưu kết quả làm baseline hiệu năng, so sánh sau mỗi
        lần upgrade hoặc đổi config. Đối chiếu `docs/vm-performance-measurement.md` và
        `docs/volume-max-performance.md` của bản Ceph.
- [ ] **20. Restore và diễn tập khôi phục** — `vitastor/operations.py` mới có `backup`.
  - [ ] Bổ sung `restore`, scheduler, retention và restore-drill tương đương `worker/backup/`.

## Thứ tự ưu tiên đề xuất

| Hạng mục | Lý do |
|---|---|
| 4.1 Xác minh sau khắc phục | Không có nó thì vòng lặp khắc phục ở mục 4 chưa dùng thật được. |
| 4.2 Tự huỷ đề xuất | Rẻ, loại bỏ rác PENDING tích tụ trong vận hành hằng ngày. |
| 13. Log Intelligence | Tận dụng trọn vẹn hạ tầng Loki/RCA vừa hoàn thành ở bản Ceph. |
| 14. Vòng đời ổ đĩa/OSD | Thao tác vận hành viên cần hằng ngày, hiện phải làm tay hoàn toàn. |
| 3. Dự báo đầy dung lượng | Mục roadmap dễ nhất còn bỏ trống — dữ liệu 30 ngày đã có sẵn. |

## Mục 1 — Thiết kế triển khai

### Dữ liệu đầu vào

Chỉ dùng kết quả lệnh đọc từ `vitastor-cli --json`: `status`, `ls-pools --stats`,
`osd-tree -l`, `ls -l`, sau đó bổ sung `pg-list` ở pha kế tiếp. Credential kết nối
không được đưa vào prompt hoặc response.

### Output bắt buộc

Chẩn đoán phải có: health, nguyên nhân có khả năng nhất, mức ảnh hưởng, confidence,
bằng chứng, các bước kiểm tra/xử lý theo thứ tự, command preview và cảnh báo an toàn.
Command preview chỉ là văn bản; endpoint chẩn đoán không có khả năng thực thi.

### Tiêu chí hoàn thành

1. Có bản ghi chẩn đoán bền vững gắn với Vitastor cluster.
2. Dashboard cho phép chạy và xem lại chẩn đoán.
3. Cả Router API, Codex và Claude dùng chung contract output.
4. AI lỗi không làm mất evidence và có trạng thái `FAILED` rõ ràng.
5. Test tự động chứng minh phân quyền sản phẩm, persistence và read-only boundary.

## Nhật ký triển khai

| Ngày | Mục | Trạng thái | Thay đổi | Kiểm thử | Commit |
|---|---:|---|---|---|---|
| 2026-08-13 | 1 | Hoàn thành bản lõi | DiagnosticRun, migration, evidence read-only, provider-neutral JSON contract, API và Dashboard UI | `25 passed` (Vitastor diagnosis/dashboard/client/monitor), JS syntax, compileall, Alembic 1 head | Chưa commit |
| 2026-08-13 | 2 | Hoàn thành | Baseline median/MAD theo entity và khung giờ, anomaly lifecycle, Telegram, API và Dashboard table | `45 passed` tập trung; JS syntax, compileall, Alembic 1 head | Chưa commit |
| 2026-08-15 | 4 | Nền tảng hoàn thành | Closed-loop remediation: model + audit + migration (head `d9a1c7b3e204`), policy SAFE/RISKY đóng, command builder + allowlist, proposer down-OSD, wiring watcher, route approve/reject/audit, thẻ Dashboard | `16 passed` (test_vitastor_remediation) + `45 passed` (monitor/dashboard/auth/client hồi quy); `py_compile` sạch; Alembic 1 head | Chưa commit |

## Ghi chú bàn giao

- Dashboard Vitastor và Watcher đang được phát triển song song; luôn kiểm tra `git status`
  và không ghi đè thay đổi chưa commit của người khác.
- Migration hiện tại có một Alembic head; migration mới phải nối từ head đang có tại
  thời điểm tạo và chạy `alembic heads` trước khi commit.
- Test đầy đủ của repository có một số lỗi hạ tầng/collection cũ; luôn ghi riêng kết quả
  test tập trung của Vitastor và lỗi ngoài phạm vi.
