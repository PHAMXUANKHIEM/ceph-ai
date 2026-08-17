# Backup & Disaster Recovery — kế hoạch triển khai và bàn giao

Tài liệu này là nguồn theo dõi chung cho phạm vi **Backup & Restore** của
`ceph-ai`. Mọi thay đổi liên quan backup policy, scheduler, storage target,
restore, restore drill, cảnh báo hoặc giao diện Backup cần cập nhật trạng thái
và nhật ký bàn giao ở cuối tài liệu.

## 1. Mục tiêu

- Chứng minh dữ liệu có thể khôi phục, không chỉ chứng minh lệnh backup đã chạy.
- Restore an toàn theo mặc định, không ghi đè production khi chưa qua preflight
  và approval rõ ràng.
- Theo dõi được RPO, RTO, số bản sao, tính toàn vẹn và tình trạng immutable.
- Mọi dữ liệu/job/audit được scope đúng theo cluster, pool, image và target.
- Quản trị policy từ Dashboard mà không cần sửa YAML thủ công.
- Backup không làm lộ secret, không chạy chồng, retry không tạo artifact trùng.

## 2. Quy ước trạng thái

- `[ ]` Chưa làm.
- `[~]` Đang làm hoặc mới hoàn thành một phần.
- `[x]` Hoàn thành code, test và tài liệu.
- `[!]` Bị chặn; phải ghi nguyên nhân trong nhật ký bàn giao.

Một mục chỉ được đánh dấu `[x]` khi đạt đủ acceptance criteria, có migration
nếu cần, test mặc định/secondary cluster và không còn đường mutation ngoài
policy/approval đã định nghĩa.

## 3. Hiện trạng đã có

- [x] Full RBD backup bằng `rbd export`.
- [x] Incremental backup bằng `rbd export-diff`.
- [x] Snapshot nguồn, streaming theo chunk, SHA-256 và tiến độ upload.
- [x] Hai storage target cố định A/B, hỗ trợ SFTP và S3/MinIO.
- [x] S3 Object Lock khi bucket đã bật immutable đúng cách.
- [x] Retention riêng cho full/incremental và bảo vệ full còn dependency.
- [x] Metadata backup: monmap, osdmap, crushmap, auth và config dump.
- [x] Scheduler bền vững qua APScheduler.
- [x] Backup thủ công và metadata backup thủ công từ Dashboard.
- [x] Anomaly detection, AI analysis, digest, webhook và Telegram alert.
- [x] Restore drill cơ bản từ full backup vào scratch image.
- [x] Restore production có approval và audit.
- [x] Restore Cluster/DR workflow và runbook.
- [~] Multi-cluster đã có dữ liệu `cluster_id`, nhưng cần audit lại toàn bộ
  dedup/in-flight query và các chức năng còn default-cluster-only.
- [~] Trang Backup có queue, progress, history, anomaly và digest nhưng chưa có
  Recovery Point/Target/Policy workspace đầy đủ.

## 4. Nguyên tắc an toàn

1. Restore mặc định phải tạo volume mới; in-place overwrite là chế độ nâng cao.
2. Không restore nếu chain thiếu, checksum sai, target không truy cập được hoặc
   pool đích thiếu dung lượng.
3. Không ghi đè/xóa khi image còn watcher, lock, snapshot hoặc clone dependency.
4. Mọi action nguy hiểm phải preview, xác nhận lại định danh, approval và audit.
5. Không coi backup thành công nếu số bản sao chưa đạt policy yêu cầu.
6. Retention/manual delete không được xóa full còn incremental phụ thuộc.
7. Immutable lock không có đường bypass trong ứng dụng.
8. Secret S3/SSH không xuất hiện trong response, log, audit hoặc proposed command.
9. Job phải idempotent và được reconcile sau timeout, crash hoặc Worker restart.
10. Mọi API và query phải fail-closed theo cluster; không fallback sang cluster
    mặc định khi cluster phụ thiếu cấu hình.

## 5. P0 — An toàn phục hồi và khả năng quan sát

### 5.1 Restore thành volume mới `[~]`

- [x] Cho phép chọn `destination_pool` và `destination_image`.
- [x] Mặc định tạo image cô lập, import full rồi apply diff chain.
- [~] Verify sau restore: đã chạy `rbd info` và dọn image đích nếu verify lỗi;
  còn thiếu đối chiếu size, checksum/read và feature compatibility đầy đủ.
- Không tự promote hoặc đổi tên production.
- Thêm action promote/swap riêng, luôn RISKY và cần approval.
- [x] Luồng mặc định trên Dashboard không còn restore đè image nguồn.
- In-place restore chỉ xuất hiện trong Advanced Mode, yêu cầu nhập lại chính xác
  `pool/image` và cảnh báo ghi đè.

Đã bổ sung action `restore_rbd_image_as_new`, approval RISKY, kiểm tra tên đích,
chặn trùng image, kiểm tra dung lượng pool cơ bản, audit destination và cleanup
best-effort khi chuỗi import/verify thất bại. Hiện vẫn chọn recovery point thành
công mới nhất; preflight sâu và re-check tại Worker thuộc mục 5.2–5.3.

**Hoàn thành khi:** một recovery point có thể được restore sang image mới mà
không thay đổi image nguồn; failure dọn sạch artifact/image chưa hoàn chỉnh và
audit ghi rõ recovery point cùng destination.

### 5.2 Recovery Point selector `[~]`

- [x] Liệt kê recovery point theo cluster/pool/image/target.
- [~] Hiển thị full/incremental, thời điểm, kích thước và target; SHA-256 cùng
  trạng thái verify cần bổ sung cột/model integrity.
- [~] Hiển thị dependency chain chính xác theo job ID; chưa có metadata để phát
  hiện sequence gap bên trong export-diff.
- [x] Cho phép chọn mốc khôi phục thay vì luôn dùng bản mới nhất.
- Phân trang, search và filter theo thời gian/target/status.

API `GET /api/backups/recovery-points` chỉ trả job SUCCESS đúng cluster và bỏ
incremental mất full base hoặc lệch target slot. Action lưu
`recovery_point_job_id`; Worker resolve lại exact full + diff đến mốc đã duyệt,
không tự nhảy sang backup mới hơn xuất hiện trong approval gap.

**Hoàn thành khi:** operator nhìn thấy chính xác full + các diff cần thiết trước
khi đề xuất restore; API từ chối recovery point không đầy đủ hoặc khác cluster.

### 5.3 Restore preflight `[~]`

- [~] Ghi nhận watcher/client, snapshot và child clone của nguồn; restore-as-new
  không chặn khi nguồn đang attach hoặc đã mất vì không mutate nguồn.
- [~] Kiểm tra backup in-flight; exclusive lock và restore resource conflict sâu
  vẫn cần bổ sung.
- [~] Kiểm tra RBD application, near-full, max available và dung lượng ước tính;
  quota chính xác và logical image size chưa có trong BackupJob.
- Kiểm tra feature/format compatibility.
- HEAD/stat toàn bộ artifact trên target và verify metadata SHA-256.
- Ước lượng dung lượng tải xuống, thời gian và RTO.
- [x] Lưu preflight evidence trong action; Worker re-check destination existence,
  RBD application, near-full và capacity ngay trước import, fail-closed nếu đổi.

**Hoàn thành khi:** mọi restore đều có preflight ở lúc propose và re-check ở lúc
execute; thay đổi trạng thái giữa hai thời điểm làm action fail-closed.

### 5.4 Dashboard RPO/RTO `[x]`

- [x] Trạng thái `Healthy`, `RPO at risk`, `RPO breached`, `Never backed up`.
- [x] Tuổi backup thành công gần nhất, RPO mục tiêu và thời gian còn lại.
- [x] Hiển thị RTO thực tế của RestoreDrill và RTO dự kiến theo kích thước
  recovery chain/tốc độ RestoreDrill thành công gần nhất; thiếu dữ liệu thì
  fail-visible thay vì đưa số giả.
- [x] Tổng số volume theo từng trạng thái và số volume không đủ bản sao;
  `required_copy_count` có mặc định toàn cục và hỗ trợ override theo workload.
- [x] RPO đọc từ `backup_policy.yaml`, hỗ trợ override theo workload và
  `Cluster.backup_rpo_hours`; Dashboard/alerting dùng cùng một ngưỡng.
- [x] Cảnh báo và hiển thị rõ metadata backup/RestoreDrill chưa chạy, thất bại
  hoặc quá hạn; drill chỉ được giám sát khi đã cấu hình đầy đủ.

**Hoàn thành khi:** overview trả lời được “volume nào không đạt RPO?”, “backup
nào chưa từng được drill?” và “nếu restore bây giờ mất khoảng bao lâu?”.

### 5.5 Audit và sửa multi-cluster scope `[ ]`

- Join `Incident.cluster_id` trong mọi query dedup/in-flight Action.
- Scope BackupJob, anomaly, digest, drill, retention và restore chain thống nhất.
- Cho phép backup ở hai cluster chạy song song khi không dùng chung resource.
- Không để action cluster A chặn hoặc đọc lịch sử cluster B.
- Ghi cluster trong audit, alert, history và artifact key/prefix.
- Liệt kê rõ tính năng còn default-cluster-only; không hiển thị như đã hỗ trợ.

**Hoàn thành khi:** test hai cluster chứng minh không cross-read/cross-block/
cross-restore và cluster inactive bị từ chối.

## 6. P1 — Hoàn thiện vận hành

### 6.1 Backup Policy Editor `[ ]`

- CRUD tracked image từ Dashboard.
- Lịch backup theo image/cluster.
- `full_refresh_every_n_days` để giới hạn độ dài chain.
- Retention full/incremental, RPO và restore-drill schedule.
- Chọn target, required copy count và immutable target.
- Preview diff, validation, versioning, audit và rollback policy.
- Reload Worker an toàn; không báo thành công giả nếu restart thất bại.

### 6.2 Test Backup Target `[ ]`

- Nút Test connection cho từng slot.
- Xác minh DNS/network/TLS/authentication.
- Upload object thử, stat/download/verify rồi dọn.
- Kiểm tra quyền read/write/delete theo policy.
- Kiểm tra S3 Object Lock/retention mode thực sự hoạt động.
- Hiển thị quota/capacity và cảnh báo hai slot trỏ cùng host/bucket/site.
- Redact access key, secret, token, SSH key path trong output/audit.

### 6.3 Target health và copy compliance `[ ]`

- Trạng thái A/B riêng cho từng run.
- Tổng hợp `2/2 healthy`, `1/2 degraded`, `0/2 failed`.
- Hiển thị immutable copy đã được lock tới ngày nào.
- Required copy count theo policy.
- Action Repair Missing Copy không cần export lại nguồn nếu còn một bản verified.
- Không đánh dấu run tổng thể SUCCESS khi chưa đạt required copy count.

### 6.4 Retry, resume, cancel và reconciliation `[ ]`

- Retry job thất bại với cùng logical request/idempotency key.
- Multipart/resumable upload khi backend hỗ trợ.
- Cancel an toàn ở export/upload/download/restore.
- Dọn `.part`, file tạm, snapshot và scratch image trong `finally`.
- Reconcile `RUNNING` sau crash/restart; không chỉ dựa vào timeout cứng.
- Bổ sung trạng thái `CANCELLED`, `STALE`, `PARTIAL_SUCCESS`, `VERIFY_FAILED`.

### 6.5 Backup inventory độc lập policy `[ ]`

- Lịch sử toàn bộ BackupJob, kể cả image đã bỏ khỏi tracked list.
- Search/filter theo cluster, pool, image, type, target, status và thời gian.
- Pagination server-side.
- Trang chi tiết job: run ID, chain, artifact, size, checksum, duration và error.
- Export CSV/JSON có redaction.
- Deep-link từ alert/digest/audit tới job liên quan.

### 6.6 Manual retention/delete `[ ]`

- Hoàn thiện executor cho `backup_delete_manual`.
- Preview recovery point, dependency và dung lượng sẽ giải phóng.
- Nhập lại artifact hoặc `pool/image` để xác nhận.
- Chặn full còn diff phụ thuộc và object đang immutable.
- Approval, audit và kết quả per-target.
- Không báo đã xóa nếu backend từ chối hoặc chỉ xóa được một phần.

## 7. P2 — Độ tin cậy nâng cao

### 7.1 Full-chain Restore Drill `[ ]`

- Drill full + toàn bộ incremental chain.
- Chọn ngẫu nhiên recovery point cũ, không chỉ bản full mới nhất.
- Drill độc lập từ từng target A/B.
- Lưu download speed, restore duration, verify duration và RTO thực tế.
- Cảnh báo khi không có drill thành công trong ngưỡng policy.
- Không dọn scratch nếu cấu hình giữ evidence phục vụ điều tra; có TTL riêng.

### 7.2 Application-consistent backup `[ ]`

- QEMU guest-agent freeze/thaw hoặc hook tích hợp hypervisor.
- Pre/post hook có timeout và output redaction.
- Luôn thaw trong failure path.
- Hook chuyên biệt cho database/workload khi cần.
- Gắn nhãn `crash-consistent` hoặc `application-consistent` vào BackupJob.
- Cảnh báo nếu policy yêu cầu application consistency nhưng hook thất bại.

### 7.3 Capacity planning `[ ]`

- Dung lượng backup theo cluster/pool/image/target.
- Tốc độ tăng trưởng ngày/tuần/tháng.
- Dự báo ngày đầy target.
- Ước lượng full tiếp theo và local temporary space cần thiết.
- Cảnh báo trước khi chạy nếu nguồn/temp/target thiếu capacity.
- Hiển thị compression/dedup ratio nếu backend cung cấp dữ liệu đáng tin cậy.

### 7.4 Alert lifecycle `[ ]`

- Dedup và cooldown theo cluster/resource/kind.
- Trạng thái `OPEN`, `ACKNOWLEDGED`, `RESOLVED`.
- Gửi lại khi severity tăng hoặc tới reminder interval.
- Recovery notification khi RPO/target/job trở lại bình thường.
- Deep-link tới job/recovery point.
- Escalation khi critical không được acknowledge trong thời gian cấu hình.

### 7.5 Credential và ransomware resilience `[ ]`

- Kiểm tra credential source và target tách biệt.
- Hỗ trợ rotation không làm gián đoạn job đang chạy.
- Kiểm tra target immutable định kỳ bằng write/delete probe có kiểm soát.
- Cảnh báo policy có hai slot nhưng cùng failure domain.
- Audit truy cập/restore/delete và retention lock.
- Runbook credential loss, target compromise và key rotation.

## 8. Kiến trúc giao diện đề xuất

Trang Backup chia thành các workspace/tab:

| Tab | Nội dung |
|---|---|
| Overview | RPO/RTO, protected/unprotected, target health, copy compliance |
| Jobs | Queue, running, history, retry, cancel và reconciliation state |
| Recovery Points | Full/diff chain, checksum, target và restore proposal |
| Restore Drills | Drill history, RTO, validation và evidence |
| Targets | Connection test, capacity, immutability và copy status |
| Policies | Tracked images, schedule, retention, RPO và target assignment |
| Alerts & Audit | Alert lifecycle, anomaly, digest và mutation audit |

Non-admin được xem dữ liệu read-only đã redact. Storage Admin được quản trị
policy và trigger backup/restore theo RBAC. Mọi mutation không nên phụ thuộc
vào việc nút bị ẩn; API phải kiểm tra quyền độc lập.

## 9. API và data model dự kiến

Các tên dưới đây là gợi ý, cần đối chiếu conventions hiện có trước khi code:

- `GET /api/backups/overview`
- `GET /api/backups/jobs`
- `GET /api/backups/jobs/{id}`
- `POST /api/backups/jobs/{id}/retry`
- `POST /api/backups/jobs/{id}/cancel`
- `GET /api/backups/recovery-points`
- `POST /api/backups/restore/preflight`
- `POST /api/backups/restore-as-new/propose`
- `POST /api/backups/promote/propose`
- `GET/PUT /api/backups/policies/{cluster_id}`
- `POST /api/backups/targets/{slot}/test`
- `POST /api/backups/recovery-points/{id}/repair-copy`
- `POST /api/backups/recovery-points/{id}/delete/propose`

Data model cần cân nhắc:

- `BackupPolicyRevision`: policy version, actor, diff và active revision.
- `BackupRecoveryPoint`: logical recovery point gom các row per-target.
- `BackupArtifact`: full/diff/metadata artifact, checksum và target state.
- `BackupTargetCheck`: connection/capacity/immutability check history.
- `BackupRestoreRun`: source recovery point, destination và pre/post evidence.
- `BackupAlertState`: dedup key, lifecycle, acknowledgement và resolution.

Ưu tiên tận dụng `BackupJob`, `Action`, `Incident` và audit hiện có; chỉ thêm
bảng khi một khái niệm không thể biểu diễn chính xác bằng schema hiện tại.

## 10. Chiến lược test

### Unit

- Chain builder: missing diff, wrong base, duplicate sequence và cross-cluster.
- Policy validation, RPO state và required copy count.
- Target probe, immutability check và redaction.
- Preflight watcher/lock/dependency/capacity/compatibility.
- Retry/cancel/reconciliation state machine.

### Integration

- Full + nhiều diff restore-as-new và verify checksum.
- Hai target: success, partial failure, repair copy và fallback restore.
- Worker crash ở export/upload/restore rồi restart/reconcile.
- Retention không xóa dependency hoặc immutable object.
- Hai cluster chạy song song không cross-read/cross-block.
- RBAC admin/non-admin và audit completeness.

### Live/Lab

- RBD thật với dữ liệu kiểm chứng trước/sau restore.
- SFTP site khác và S3/MinIO có Object Lock.
- Guest freeze/thaw với VM test.
- Full-chain drill từ từng target.
- Network interruption, target full, credential revoked và Worker restart.
- Đo RTO thực tế, cleanup snapshot/temp/scratch và absence of secret leakage.

## 11. Thứ tự triển khai

1. Audit/sửa multi-cluster scope và định nghĩa recovery point/chain.
2. Recovery Point selector + restore preflight.
3. Restore-as-new + post-verify.
4. Promote/swap workflow và giữ in-place restore ở Advanced Mode.
5. RPO/RTO overview và policy-driven alert threshold.
6. Target test/health/copy compliance.
7. Policy Editor.
8. Retry/cancel/reconciliation và inventory đầy đủ.
9. Manual delete an toàn.
10. Full-chain drill, application consistency và capacity planning.

## 12. Tiêu chí hoàn thành Data Protection milestone

- Mọi tracked image đạt RPO hoặc có alert đang mở.
- Mọi recovery point đạt required copy count và có ít nhất một verified copy.
- Ít nhất một immutable target được kiểm chứng định kỳ.
- Restore-as-new từ một full+diff chain hoàn thành và post-check đạt.
- Restore drill full-chain gần nhất nằm trong ngưỡng policy.
- Không có cross-cluster query/action/artifact.
- Crash/restart không để job `RUNNING` vĩnh viễn hoặc artifact tạm không quản lý.
- Operator có thể cấu hình policy, xem chain, test target và restore mà không sửa
  YAML hay SSH vào application server.
- Toàn bộ mutation có actor, request/action ID, cluster, resource và kết quả audit.

## 13. Nhật ký bàn giao

| Ngày | Phạm vi | Trạng thái | Thay đổi | Kiểm tra | Việc tiếp theo |
|---|---|---|---|---|---|
| 2026-08-17 | Audit/kế hoạch | Hoàn thành | Rà soát Dashboard route/template, backup engine, scheduler, policy, storage backend, restore/drill, alerting, model và tài liệu hiện có; tạo roadmap P0–P2. | Review code/tài liệu; chưa thay đổi runtime. | Bắt đầu mục 5.5 multi-cluster audit, sau đó định nghĩa Recovery Point cho 5.2. |
| 2026-08-17 | Restore-as-new nền tảng | Đang triển khai | Thêm propose API, destination preflight cơ bản, action/approval, executor full+diff, `rbd info` post-check, cleanup khi lỗi và chuyển nút Restore sang mặc định an toàn. | 277 test liên quan passed; JS check, Python compile và diff check đều đạt; chưa kiểm chứng trên cụm Ceph thật. | Hoàn thiện post-verify sâu, Worker re-check, Recovery Point selector và Advanced Mode cho in-place restore. |
| 2026-08-17 | Recovery Point selector nền tảng | Đang triển khai | Thêm API cluster-scoped, chain job IDs, UI chọn mốc và khóa exact recovery point vào action/Worker để không dịch chuyển trong approval gap. | Regression mở rộng 282 passed; JS, Python compile và diff check đạt. | Bổ sung SHA-256/verify metadata, gap detection, filter/pagination và UX modal thay prompt. |
| 2026-08-17 | Restore preflight nền tảng | Đang triển khai | Lưu evidence nguồn/đích/chain/capacity vào action; chặn destination tồn tại, pool near-full/RBD-disabled, thiếu capacity và backup in-flight; Worker re-check ngay trước import. | Regression mở rộng 284 passed; JS, Python compile và diff check đạt (một lỗi fixture SQLite ngẫu nhiên biến mất khi chạy lại riêng). | Thêm artifact HEAD/SHA-256, feature compatibility, quota/logical size và RTO estimate. |

## 14. Quy tắc cập nhật tài liệu

- Mỗi lượt triển khai cập nhật checklist và thêm một dòng nhật ký bàn giao.
- Ghi rõ test đã chạy, số passed/failed và giới hạn chưa kiểm chứng live.
- Không đánh dấu hoàn thành chỉ vì đã có UI; phải có executor/guard/audit/test.
- Nếu thay đổi thiết kế restore, retention hoặc immutable, cập nhật đồng thời
  `docs/ceph-backup.md` và `docs/runbook-dr.md`.
- Không ghi secret, IP credential-bearing hoặc dữ liệu production vào tài liệu.
