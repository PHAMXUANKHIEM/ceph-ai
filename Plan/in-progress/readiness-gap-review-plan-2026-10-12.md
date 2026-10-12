# Kế hoạch: đóng khoảng cách sẵn sàng production (đánh giá độc lập 12/10/2026)

**Ngày lập:** 12/10/2026
**Trạng thái:** `IN-PROGRESS`. Làm lần lượt từ R1.
**Nguồn:** bản đánh giá độc lập "Ceph-AI so với croit" chấm `origin/main` 76dddd6e (CI run 38158238496 xanh),
chỉ đọc code, tài liệu và tài liệu croit công khai, không truy cập runtime. Điểm 47/100; kết luận: **lab nội bộ /
pilot có người giám sát**, NO-GO cho remediation diện rộng hoặc phát hành cho khách hàng đến khi đóng P0/P1.
**Liên quan:** `croit-parity-operations-plan-2026-10-10.md` (C1–C6, tính năng),
`strict-production-readiness-closure-plan-2026-09-25.md` (backup/migration/rollback; R3 dùng lại các mục ở đó),
`docs/multi-cluster-feature-scope-remediation.md` (checklist đa cụm; R1 đóng các mục còn mở).

## 1. Đối chiếu bản đánh giá với hệ thống đang chạy

### Đúng (đã kiểm tra trên main)

| Nhận định | Bằng chứng |
|---|---|
| Single-node, không HA, chưa đo RTO/RPO | `docs/operations/reliability-slo.md` |
| Soak 24 giờ chưa đạt | `docs/benchmark/reliability-soak-2026-09-25.json`: `exit_gate = REVIEW_REQUIRED`, chưa ai ký |
| Checklist đa cụm còn mở | `multi-cluster-feature-scope-remediation.md` 7.1, 7.2, 7.4, 9.1, 9.4, 9.5 |
| Sao chép RBD chỉ đọc | `dashboard/routes/volumes.py`: `mutation_supported: False` |
| Dashboard không có SSO | `dashboard/routes/auth.py` chỉ có tài khoản cục bộ |
| License chỉ dùng nội bộ | `LICENSE` |

**P0.1 đã có bằng chứng thật.** Lần thử C5 đầu tiên trên CS-STAGING (11/10) lộ hai lỗi đa cụm: Worker từ chối
mọi hành động trên cụm phụ vì cụm lưu khoá chỉ đọc (#61), và Watcher tự đóng yêu cầu operator đang chờ duyệt
như health check đã hồi phục (#63). Cả hai đã sửa, nhưng cho thấy ma trận cụm chưa từng được kiểm thử.

### Đã cũ hoặc thiếu (bản đánh giá không thấy runtime / PR chưa merge)

| Nhận định | Thực tế 12/10 |
|---|---|
| Không có staging | CS-STAGING (3 node cephadm, fsid `1beef4a8-…`) là cụm staging của Failure Lab; CS-LAB là `production` |
| CephFS/NFS/RGW chưa chạy end-to-end | 5 thao tác C5 đã chạy trên CS-STAGING ngày 11/10: CephFS `fsstaging` (MDS active/standby), subvolume `app1` 2 GiB, NFS `nfsstaging` (cổng 2049), export `/app1`, RGW `s3staging` (S3 trả lời). **Lỗi phát hiện:** export trỏ gốc filesystem (`path: /`) thay vì subvolume |
| Chưa có bảo trì / thêm node | Có code: #56 (C2 bảo trì, reboot lần lượt), #57 (C3 thêm node) — chưa merge |
| Chưa có báo cáo vận hành / leo thang | #60 (C6) — chưa merge |
| Thay OSD chưa nghiệm thu | **Đúng.** C1 đã merge, chưa chạy trên CS-STAGING |
| AI tự học chưa chứng minh | **Đúng, và tệ hơn:** 22/30 đề xuất thăng cấp model dựa trên đánh giá cũ 19–22 ngày; mỗi node CS-LAB bị học hai lần (IP 10.3.x và 10.20.1.x). Đang sửa: `cand/promotion-guard-fresh-best`, `cand/node-resources-per-machine` |
| — | Học từ log: CS-LAB ~90% lượt ingest PARTIAL (sửa ở #53), CS-STAGING 100% FAILED vì chưa có log shipper (đang cài Alloy) |

### Chỗ không đồng ý về mức ưu tiên

P1.4 (RBD mirroring), P1.5 (RGW multisite) và P1.6 (license/support) chỉ là P1 **nếu mục tiêu là bán cho khách
hàng**. Với mục tiêu công cụ nội bộ, chúng là P2/P3. Cần chủ dự án chốt mục tiêu (R7) trước khi đầu tư.

## 2. Nguyên tắc

- Mỗi gói một nhánh `cand/*` + PR; hành động mới ở mức RISKY.
- "Hỗ trợ" chỉ được ghi trong `docs/ceph-capability-matrix.md` sau khi có **acceptance trên CS-STAGING**.
- Mỗi acceptance có run report: SHA đang deploy, cụm, thời gian, trạng thái Ceph trước/sau (`ceph -s`,
  `health detail`), output từng bước, lỗi cố ý gây ra (nếu có), người chứng kiến. Lưu ở
  `docs/acceptance/<ngày>-<tên>.md`.
- Không đánh dấu xong gói nào chỉ vì CI xanh.

## 3. Gói việc

### R1 — Gắn chặt cụm đích từ đề xuất đến thực thi (P0.1)
- [ ] Mỗi Action mang `cluster_id` bất biến (lấy từ Incident lúc tạo); Worker từ chối khi `cluster_id` của
  Action, Incident, credential và node đích không khớp, hoặc cụm inactive/đã xoá. Không bao giờ rơi về cụm mặc
  định khi thiếu `cluster_id` của cụm phụ.
- [ ] Danh sách node và kiểm tra lệnh (`target_nodes`, allowlist) theo cụm của Action (mục 7.2).
- [ ] Phiên chat / tin nhắn gắn cluster scope (7.1); Telegram NL dùng cụm đã chọn.
- [ ] Header các trang lớn hiển thị cụm backend thực sự đang dùng (9.1).
- [ ] Ma trận test (9.4): cụm mặc định, cụm phụ đang bật, cụm phụ tắt; cluster ID giả mạo, đổi target sau khi
  duyệt, duyệt hết hạn, replay idempotency key, Worker restart giữa chừng.
- [ ] Ma trận chạy thật: một hành động SAFE và một RISKY trên CS-LAB và CS-STAGING; audit ghi đúng cụm.
- **Nghiệm thu:** mọi ca trong ma trận bị từ chối hoặc chạy đúng cụm; checklist 7.x/9.x đóng và ghi kết quả (9.5).

### R2 — Nghiệm thu thao tác vòng đời trên CS-STAGING (P1.3, phần còn lại của P1)
- [ ] C5: sửa export NFS trỏ đúng subvolume (`ceph fs subvolume getpath`), tạo lại export `/app1`, mount thử
  từ một client; sửa/xoá/restart dịch vụ (CRUD tối thiểu) hoặc ghi rõ giới hạn.
- [ ] C1 thay OSD: preview → `osd rm --replace` → zap → tạo lại cùng ID → `active+clean`; gây lỗi giữa hai
  bước (ngắt SSH, OSD không lên) và chứng minh dừng an toàn / làm tiếp được.
- [ ] C2 bảo trì (sau #56): ceph-3 → ceph-2 → ceph-1; một lần cố ý để node không về để kiểm tra dừng an toàn.
- [ ] C3 thêm node (sau #57): cần một VM trống; nếu chưa có, ghi rõ chưa nghiệm thu.
- [ ] Nâng cấp Ceph minor trên CS-STAGING qua cổng C4 (soak 24 giờ) trước CS-LAB.
- **Nghiệm thu:** mỗi thao tác có run report; capability matrix cập nhật theo kết quả.

### R3 — Diễn tập phục hồi control plane (P0.2, P1.1)
Dùng lại các mục còn mở của `strict-production-readiness-closure-plan-2026-09-25.md`; ở đây chỉ chốt bằng chứng.
- [ ] Backup PostgreSQL ra ngoài host; restore trên host khác; đo RPO/RTO.
- [ ] Restart RabbitMQ, Worker, Dashboard lúc có Action đang chạy: Action không mất, không chạy hai lần, audit
  còn nguyên (kiểm tra idempotency key và trạng thái EXECUTING sau restart).
- [ ] Deploy cùng một release qua migration 3 lần; rollback image được chứng kiến; smoke test.
- [ ] Chạy lại soak 24 giờ và ký nghiệm thu (hoặc ghi lý do không đạt).
- [ ] Ghi RTO/RPO đo được vào `reliability-slo.md`; quyết định single-node có công bố hay làm HA (đưa vào R7).
- **Nghiệm thu:** báo cáo gắn SHA, thời gian, output, người xác nhận.

### R4 — Giới hạn phạm vi ảnh hưởng của remediation (Pha 0)
- [ ] Rà mọi hành động RISKY/SAFE: có preview, preflight, timeout, dedupe, post-check và đường phục hồi; liệt
  kê hành động thiếu bước nào.
- [ ] Fault injection trên CS-STAGING (dùng Failure Lab FL6.x): lỗi giữa các bước của hành động nhiều pha.
- **Nghiệm thu:** bảng hành động × bước an toàn đầy đủ; mỗi hành động thiếu bước có issue hoặc đã sửa.

### R5 — Bằng chứng AI (P2.2, P2.3)
- [ ] Deploy hai bản sửa ngày 12/10 (guard thăng cấp model; mỗi máy học một lần), log shipper cho CS-STAGING và
  #53; xác nhận lượt ingest hết PARTIAL/FAILED.
- [ ] Golden incident set: incident có nhãn operator độc lập, chia theo thời gian và cụm; manifest có phiên bản.
- [ ] Benchmark RCA và dự báo so với baseline (rule/RCA hiện có), có precision/recall, abstention, calibration,
  khoảng tin cậy; báo cáo theo horizon/scope.
- [ ] Failure Lab: chỉ tính pass khi có run report (SHA, trạng thái trước/sau, cleanup, recovery, người chứng kiến).
- [ ] Không gọi "AI tự học tốt hơn" trong tài liệu khi chưa có kết quả trên.

### R6 — SSO/RBAC cho Dashboard (P1.2)
- [ ] OIDC hoặc LDAP cho đăng nhập Dashboard; map group → role; quyền theo cụm; thu hồi phiên; audit actor.
- [ ] Test: login/logout/hết hạn, user bị khoá, từ chối truy cập cụm không có quyền, CSRF/session.
- Phụ thuộc: IdP thử nghiệm; R1 (quyền theo cụm dựa trên cluster binding).

### R7 — Quyết định của chủ dự án (P1.6, phạm vi P1.4/P1.5, HA)
- [ ] Mục tiêu: công cụ nội bộ hay sản phẩm cho khách hàng.
- [ ] Nếu là sản phẩm: license, ma trận phiên bản Ceph/OS hỗ trợ, SBOM, quy trình CVE, EOL, support/escalation.
- [ ] HA hay công bố single-node cho pilot.
- [ ] Ưu tiên RBD mirroring/DR, RGW multisite, SMB/NVMe-oF, HA NFS/S3 theo nhu cầu đã xác nhận.
- Việc rẻ làm ngay bất kể quyết định: trang sao chép RBD ghi rõ "chỉ đọc / RPO ước tính".

## 4. Thứ tự

1. R1 (P0.1) — đã có bằng chứng lỗi thật, ảnh hưởng mọi thao tác trên cụm phụ.
2. R2 — song song với R1 trên CS-STAGING, vì các acceptance cũng là ma trận chạy thật của R1.
3. R3 và R4 (P0.2 và Pha 0).
4. R5, rồi R6.
5. R7 cần quyết định của chủ dự án; không chặn R1–R5.

## 5. Theo dõi điểm

Chấm lại theo đúng công thức của bản đánh giá (30% quản trị Ceph, 20% AI, 20% an toàn, 20% vận hành, 10%
phát hành) sau mỗi gói đóng; ghi điểm và bằng chứng vào cuối file này.

| Ngày | Điểm | Thay đổi |
|---|---:|---|
| 12/10/2026 | 47 | Mốc từ bản đánh giá độc lập |
