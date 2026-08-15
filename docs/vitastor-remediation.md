# Vitastor — Vòng lặp khắc phục đóng (closed-loop remediation)

Tài liệu này mô tả tính năng đưa sản phẩm Vitastor lên ngang hàng với Ceph ở
khả năng **AIOps** cốt lõi: không chỉ *phát hiện/chẩn đoán* sự cố (đã có ở
mục 1–2 của roadmap) mà còn *đề xuất → phân loại → phê duyệt → thực thi → ghi
audit* hành động khắc phục — đúng kiến trúc pipeline `Incident → Action →
AuditEntry` của Ceph, nhưng cô lập hoàn toàn trong package `vitastor`.

> Trước tính năng này, AI Vitastor chỉ *đọc* và *nói* — `commands_preview`
> trong chẩn đoán là văn bản, không bao giờ chạy. Giờ đây một tín hiệu lỗi
> quan sát được (một OSD `up:false`) trở thành một đề xuất khắc phục **chờ
> duyệt**, có thể được thực thi qua SSH và để lại dấu vết audit đầy đủ.

## 1. Nguyên tắc an toàn (giống hệt pipeline Ceph)

Mọi quyết định thực thi đều tuân thủ đúng các bất biến an toàn của bản Ceph:

1. **`action_id` là enum đóng** (`vitastor/remediation.py::VALID_ACTION_IDS`) —
   không bao giờ parse shell tự do. Mỗi lệnh do một builder trong
   `_COMMAND_BUILDERS` sinh ra.
2. **Phân loại bảo thủ mặc định (AD-5)** — SAFE *chỉ* khi trúng allowlist
   `SAFE_ACTION_IDS`; mọi thứ khác (kể cả `action_id` không nhận diện được)
   đều RISKY và bắt buộc chờ duyệt.
3. **Chỉ chạy trên host thuộc cụm** — `run_remediation` từ chối bất kỳ host nào
   không nằm trong `known_hosts(cluster)` (management host + node deploy +
   parent của OSD trong telemetry). Đây là cùng lớp phòng vệ như
   `dashboard/routes/vitastor.py::_cluster_log_hosts`.
4. **Không tự động chạy hành động nguy hiểm** — proposer tất định của Watcher
   hiện chỉ sinh `restart_osd_service` (RISKY). Không có gì tự thực thi lên cụm
   thật mà không có người duyệt.
5. **Không rò rỉ credential** — evidence/rationale không chứa SSH key/token;
   lệnh preview chỉ chứa id OSD dạng số đã được validate.

## 2. Luồng end-to-end

```
Watcher poll  ──> vitastor_monitor.poll_cluster_once
                    │
                    ├─ query_dashboard (read-only telemetry)
                    │
                    └─ reconcile_monitor_proposals(cluster, datasets, summary)
                         │
                         ├─ propose_from_status  (OSD up:false → restart_osd_service)
                         ├─ dedup theo dedup_key vs các action đang mở
                         ├─ SAFE  → _auto_execute (chạy ngay, audit AUTO_EXECUTED)
                         └─ RISKY → tạo PENDING_APPROVAL + audit PROPOSED
                                     → trả về danh sách để gửi Telegram alert

Dashboard  ──> GET /vitastor/api/actions?cluster_id=...   (thẻ "Khắc phục sự cố")
              POST /vitastor/api/actions/{id}/approve      (Vitastor admin)
                    │  status → APPROVED (audit)
                    └─ BackgroundTask _execute_approved
                         status → EXECUTING (audit) → run_remediation (SSH)
                         → EXECUTED / FAILED (audit + output/error)
              POST /vitastor/api/actions/{id}/reject        → REJECTED (audit)
              GET  /vitastor/api/audit?cluster_id=...        (thẻ "Nhật ký hành động")
```

## 3. Dữ liệu

Hai bảng mới, cô lập khỏi Ceph (`cluster_id` là `String` thuần, không phải FK
tới `clusters` — đúng quy ước của mọi bảng `vitastor_*`):

| Bảng | Vai trò |
|---|---|
| `vitastor_remediation_actions` | Một đề xuất khắc phục + vòng đời của nó (classification, status, target_host, action_params, proposed_command, result_output, dedup_key, approved_by, …). |
| `vitastor_audit_entries` | Nhật ký chỉ-ghi (append-only) cho mọi chuyển trạng thái — tương đương `AuditEntry` của Ceph. |

Enum trạng thái (`shared/models.py`):

- `VitastorActionClassification`: `SAFE`, `RISKY`.
- `VitastorActionStatus`: `PENDING_APPROVAL` → `APPROVED` → `EXECUTING` →
  `EXECUTED` (nhánh RISKY được duyệt); `AUTO_EXECUTED` (nhánh SAFE tự chạy);
  `REJECTED`; `FAILED`.

Migration: `alembic/versions/d9a1c7b3e204_create_vitastor_remediation.py`
(nối từ head `c7d4e5f6a701`, kiểm tra bằng `alembic heads` → 1 head duy nhất
`d9a1c7b3e204`).

## 4. Enum `action_id` và phân loại

| `action_id` | Lệnh (builder) | Phân loại |
|---|---|---|
| `resync_time` | `chronyc makestep || systemctl restart chrony/chronyd/systemd-timesyncd` | **SAFE** |
| `start_osd_service` | `systemctl start vitastor-osd@<id>` | RISKY |
| `restart_osd_service` | `systemctl restart vitastor-osd@<id>` | RISKY |
| `restart_mon_service` | `systemctl restart vitastor-mon` | RISKY |
| `restart_etcd_service` | `systemctl restart vitastor-etcd` | RISKY |
| `investigate_manually` | *(no-op — ghi nhận đã xử lý thủ công)* | RISKY |

`osd_id` được validate bằng regex `^[0-9]{1,10}$` trước khi ghép vào tên unit
systemd — một giá trị telemetry không bao giờ chạm shell dưới dạng nào khác
ngoài một con số.

Muốn đổi một hành động sang tự chạy: thêm `action_id` vào `SAFE_ACTION_IDS`.
Đây là quyết định vận hành, không phải kiến trúc — cùng tinh thần với
`worker/policy/action_policy.yaml` bên Ceph.

## 5. Các file

| File | Thay đổi |
|---|---|
| `shared/models.py` | +`VitastorActionClassification`, `VitastorActionStatus`, `VitastorRemediationAction`, `VitastorAuditEntry`. |
| `alembic/versions/d9a1c7b3e204_*.py` | Migration tạo 2 bảng. |
| `vitastor/remediation.py` | **Mới** — policy, command builder, proposer, executor, audit, reconcile. |
| `watcher/vitastor_monitor.py` | Gọi `reconcile_monitor_proposals` trong `poll_cluster_once`, cảnh báo Telegram khi có RISKY chờ duyệt (bọc try riêng, không làm hỏng poll). |
| `dashboard/routes/vitastor_actions.py` | **Mới** — API list/approve/reject/audit + BackgroundTask thực thi. |
| `dashboard/app.py` | Đăng ký `vitastor_actions.router`. |
| `dashboard/templates/vitastor/index.html` | Thẻ "Khắc phục sự cố" + "Nhật ký hành động". |
| `dashboard/static/vitastor_dashboard.js` | Render + nút Duyệt/Từ chối + nạp theo chu kỳ 30s. |
| `dashboard/static/style.css` | Style thẻ remediation (theme tối Vitastor). |
| `tests/test_vitastor_remediation.py` | **Mới** — 16 test: policy, builder, proposer, executor allowlist, reconcile/dedup, route approve/reject/audit, gating admin + cô lập sản phẩm. |

## 6. Kiểm thử

```
pytest tests/test_vitastor_remediation.py -q          # 16 passed
pytest tests/test_vitastor_monitor.py \
       tests/test_dashboard_vitastor_dashboard.py \
       tests/test_dashboard_auth.py \
       tests/test_vitastor_client.py -q               # 45 passed (hồi quy)
```

> Lưu ý môi trường: nếu `.env` cục bộ còn key cũ `TEST_RUNNER_FRONTEND_URL`
> (tính năng test-runner đã bị gỡ ở thượng nguồn), `Settings` với
> `extra="forbid"` sẽ báo lỗi khi import. Xoá dòng đó khỏi `.env` (nó cũng
> chặn Dashboard/Watcher khởi động). Bộ test chạy sạch với `.env` rỗng vì mọi
> Settings đều có default cho bản deploy mới.

## 7. Bước tiếp theo (chưa làm)

- Phê duyệt bằng **nút bấm trên Telegram** (hiện mới chỉ *cảnh báo* có đề xuất
  chờ duyệt; approve/reject vẫn qua Dashboard) — mở rộng
  `dashboard/telegram_approval_bot.py` để quét thêm bảng remediation.
- **Tự huỷ** đề xuất PENDING khi tín hiệu đã hết (OSD `up` trở lại) thay vì để
  operator từ chối thủ công.
- `dry-run` / `reweight` OSD và theo dõi rebalance (mục 4 roadmap đầy đủ).
- Sinh đề xuất từ `VitastorDiagnosticRun` (source=`DIAGNOSIS`) và
  `VitastorAnomalyEvent` (source=`ANOMALY`), không chỉ từ tín hiệu status.
