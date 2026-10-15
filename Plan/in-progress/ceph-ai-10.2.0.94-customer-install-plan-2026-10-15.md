# Kế hoạch: đưa Ceph AI trên 10.2.0.94 lên bản mới và nối federation

**Ngày lập:** 15/10/2026
**Trạng thái:** `IN-PROGRESS`.
**Người thực hiện:** một AI agent chạy **ngay trên 10.2.0.94**, do operator giao việc.
Máy Ceph AI chính (10.3.55.213) không SSH được sang 10.2.0.94 vì hai máy ở hai mạng nội bộ khác nhau,
nên mọi bước ở đây làm tại chỗ.
**Liên quan:** `readiness-gap-review-plan-2026-10-12.md` mục R8 (R8.1 bộ cài offline, R8.3 gửi số liệu),
`docs/immutable-production-release.md`, `federation/schema.sql` (PR #95),
`shared/federation_client.py` (PR #96).

## 1. Bối cảnh

- 10.2.0.94 đang chạy **một bản Ceph AI cũ**. Chưa biết bản nào, cài theo cách nào, dùng DB gì và đang
  giám sát cụm Ceph nào. Pha 1 phải trả lời các câu hỏi này trước khi đụng vào bất cứ thứ gì.
- 10.2.0.94 đóng vai **khách hàng đầu tiên** của mô hình R8:
  - khách tự chạy Ceph AI với DB riêng tại chỗ;
  - nhận bản mới bằng **bộ cài offline**, hoặc qua GitHub/GHCR nếu máy ra được internet;
  - nếu ra được WAN, máy **kích hoạt federation bằng mã** rồi gửi số liệu tổng hợp về
    `45.124.93.83:1207` (PostgreSQL, DB `ceph_ai_federation`).
- Bản mới phải có trang `/federation` (PR #96). Nếu bản được giao chưa có trang này thì bỏ qua pha 5.

## 2. Quy tắc bắt buộc

1. **Không thay đổi cụm Ceph** mà Ceph AI trên 10.2.0.94 đang giám sát:
   - không chạy `ceph osd …`, `ceph orch …`, không reboot node;
   - không duyệt action nào trên Dashboard hoặc Telegram.

   Chỉ được đọc: `ceph -s`, `ceph health detail`, `ceph versions`.
2. **Trước khi cập nhật, tắt Autopilot / tự sửa** nếu đang bật, ghi lại giá trị cũ, và bật lại sau khi
   operator xác nhận.
3. **Sao lưu trước khi đổi bất cứ thứ gì** (pha 2). Chưa có bản sao lưu đã kiểm tra được thì dừng.
4. **Không in, không ghi vào log, không commit bí mật**: mật khẩu DB, token Telegram, khoá SSH, mã kích hoạt,
   mật khẩu `fed_enroll`, nội dung `federation.json`. Khi cần kiểm tra thì chỉ in độ dài hoặc checksum.
5. **Không `source` file `.env`**: giá trị không có nháy sẽ bị shell chạy như lệnh. Đọc từng khoá bằng
   `grep '^KEY=' file | cut -d= -f2-`.
6. **Không thêm khoá mới vào `.env` trước khi image định nghĩa khoá đó đang chạy.** Settings dùng
   `extra="forbid"`, nên một khoá lạ làm container crash-loop.
7. Không `pkill -f` / `pgrep -f` với mẫu trùng chính dòng lệnh đang chạy.
8. Các thao tác **dừng dịch vụ, chạy migration, xoá container hoặc image** cần **operator đồng ý** trước,
   kèm kế hoạch rollback đã viết sẵn.
9. Không mở cổng mới ra ngoài trên 10.2.0.94. Federation chỉ cần **đi ra** tới `45.124.93.83:1207/tcp`.

## 3. Các pha

### Pha 1 — Khảo sát (chỉ đọc) → báo cáo cho operator
- [ ] Hệ điều hành, CPU/RAM/đĩa trống (`df -h`, chú ý thư mục chứa image podman/docker và `/var/lib/ceph-ai`).
- [ ] Ceph AI đang chạy thế nào:
  - `podman ps -a` hoặc `docker ps -a`; tên container, image, tag/digest, thời gian chạy;
  - có checkout git không (`git -C <dir> log -1`, `git status --short`), ở thư mục nào;
  - unit systemd liên quan (`systemctl list-units | grep -i ceph-ai`);
  - file cấu hình: `/var/lib/ceph-ai/config/.env` hoặc `.env` trong checkout. Chỉ liệt kê **tên khoá**,
    không in giá trị.
- [ ] DB: PostgreSQL ngoài, PostgreSQL trong container hay SQLite (`/var/lib/ceph-ai/ceph-ai.db`)? Ghi phiên
  bản PostgreSQL, kích thước DB, revision Alembic hiện tại (`alembic_version`).
- [ ] RabbitMQ: chạy ở đâu, phiên bản.
- [ ] Ceph AI đang giám sát những cụm nào (số cụm, `ceph -s` chỉ đọc). Có action nào đang
  `PENDING_APPROVAL`/`EXECUTING` không (nếu có thì không cập nhật lúc này).
- [ ] Kết nối mạng đi ra:

  ```sh
  for t in github.com:443 ghcr.io:443 pkg-containers.githubusercontent.com:443 45.124.93.83:1207; do
    timeout 5 bash -c "</dev/tcp/${t%:*}/${t#*:}" && echo "$t OK" || echo "$t FAIL"; done
  ```
- [ ] Viết báo cáo khảo sát vào `docs/operations/10.2.0.94-survey-<ngày>.md` hoặc gửi operator. Kết luận
  chọn một trong hai đường:
  - **Đường A (online)**: ra được `github.com` + `ghcr.io` → cập nhật theo
    `docs/immutable-production-release.md`.
  - **Đường B (offline)**: không ra được → chờ operator mang **bộ cài offline** (R8.1) sang.

### Pha 2 — Sao lưu (cần operator đồng ý nếu phải dừng dịch vụ)
- [ ] Dump DB:
  - PostgreSQL: `pg_dump -Fc`, rồi kiểm tra bằng `pg_restore --list`;
  - SQLite: `sqlite3 <db> ".backup '<file>'"`, rồi `PRAGMA integrity_check`.
- [ ] Sao lưu `/var/lib/ceph-ai/config/` (gồm `.env`), `compose.yaml` đang dùng, tên và digest của image
  đang chạy (`podman image inspect`). Ghi `sha256sum` của từng file.
- [ ] Lưu bản sao lưu ở chỗ **ngoài thư mục sẽ bị ghi đè**, ví dụ `/root/ceph-ai-backup-<ngày>/`, quyền 0700.
- [ ] Ghi sẵn **cách quay lại** vào báo cáo:
  - image cũ (giữ lại, không xoá);
  - lệnh khởi động lại bản cũ;
  - lệnh khôi phục DB.

### Pha 3 — Cập nhật lên bản mới
Phiên bản đích do operator chỉ định (một commit `main` đã qua CI, digest `ghcr.io/phamxuankhiem/ceph-ai@sha256:…`).

**Đường A (online)**
- [ ] `podman login ghcr.io` bằng token `read:packages` do operator cấp. Lệnh cần TTY thật, nên operator
  tự gõ.
- [ ] Nếu có checkout git: `git fetch`, kiểm tra checkout sạch, rồi làm đúng `docs/immutable-production-release.md`
  (`deploy_preflight.sh` → `restart_container_stack.sh` với `CEPH_AI_IMAGE=<digest>` và `DEPLOY_REF=<sha đủ 40 ký tự>`).
  Script tự sao lưu DB, chạy migration, khởi động và kiểm tra.
- [ ] Nếu bản cũ không có checkout git hoặc quá cũ so với script deploy: dừng lại, báo operator, chuyển sang
  đường B.

**Đường B (offline)**: bộ cài `ceph-ai-<version>.tar` do phía mình đóng gói (R8.1), operator mang sang.
- [ ] Kiểm tra `sha256sum -c SHA256SUMS` trước khi dùng bất cứ thứ gì trong bộ cài.
- [ ] Đọc `README` của bộ cài, rồi chạy `upgrade.sh` theo hướng dẫn. Script này làm:
  `podman load` → so revision label → sao lưu DB → migration → khởi động → smoke test.
- [ ] Nếu bộ cài chưa có `upgrade.sh`, hoặc script lỗi: **không tự viết thay thế trên máy khách**. Ghi lại
  lỗi, đưa hệ thống về bản cũ nếu đã dừng, rồi báo operator.

**Kiểm tra sau cập nhật (cả hai đường)**
- [ ] Mọi container `healthy`; không container nào restart liên tục (`podman ps`, số lần restart).
- [ ] Đăng nhập Dashboard được. Trang tổng quan hiện đúng các cụm như trước khi cập nhật.
- [ ] Watcher vẫn nhận health của từng cụm (incident mới hoặc trạng thái cập nhật trong 10 phút).
- [ ] So số dòng các bảng chính trước và sau (`clusters`, `incidents`, `actions`): không mất dữ liệu.
- [ ] Bật lại Autopilot theo giá trị cũ **sau khi operator đồng ý**.

### Pha 4 — Gói tri thức (R8.2, khi bộ cài có)
- [ ] Nếu bộ cài có `knowledge-pack-<version>.json`: nhập theo `README`, rồi ghi số mục đã nhập.
- [ ] Xác nhận gói không làm thay đổi độ tin cậy Autopilot của cụm tại chỗ.

### Pha 5 — Kích hoạt federation (chỉ khi `45.124.93.83:1207` OK ở pha 1 và bản mới có `/federation`)
- [ ] Operator đưa **file mật khẩu `fed_enroll`** (có trong bộ cài, hoặc gửi riêng). Đặt file vào
  `/var/lib/ceph-ai/config/federation-enroll.pass`, quyền 0600, chủ sở hữu là user của container Dashboard.
  Không in nội dung file.
- [ ] Operator đưa **mã kích hoạt** `CAI-XXXX-XXXX-XXXX-XXXX-XXXX`: dùng một lần, có hạn, cấp riêng cho
  10.2.0.94. Admin nhập mã ở Dashboard → **Federation** → **Kích hoạt**.
- [ ] Trang hiện "Đã đăng ký" với mã khách 12 ký tự và gói `basic`. Ghi mã khách vào báo cáo. Mã khách
  không phải bí mật; mã kích hoạt thì có.
- [ ] Đọc khung "Dữ liệu sẽ được gửi". Xác nhận chỉ có số đếm: không có tên cụm, IP, hostname, fsid, log
  hay lệnh. Thấy bất kỳ thứ gì nhận diện được thì **dừng, không bấm Gửi**, rồi báo operator.
- [ ] Bấm **Gửi**. Trang ghi "Lần gửi gần nhất" và không có lỗi. Báo operator giờ gửi để bên mình kiểm tra
  bảng `inbox.c_<mã khách>`.
- [ ] Lỗi "Mã kích hoạt không hợp lệ" nghĩa là mã sai, đã dùng hoặc hết hạn: xin mã mới, **không thử lại
  nhiều lần**. Server tự chặn IP sau 5 lần sai trong 10 phút, chặn trong 1 ngày.

### Pha 6 — Báo cáo cuối
- [ ] Viết `docs/operations/10.2.0.94-upgrade-<ngày>.md` gồm:
  - bản cũ → bản mới (sha/digest);
  - đường A hay B;
  - thời gian dừng dịch vụ;
  - số dòng các bảng trước và sau;
  - kết quả kiểm tra;
  - kết quả federation (mã khách, giờ gửi);
  - mọi lỗi gặp và cách xử lý;
  - những gì cần sửa trong bộ cài hoặc script.
- [ ] Đánh dấu các mục ở trên và ghi nhật ký bên dưới.

## 4. Phía máy chính (không phải việc của agent trên 10.2.0.94)
- R8.1: đóng gói bộ cài offline (image `podman save` theo digest, `compose.yaml`, `install.sh`/`upgrade.sh`,
  mẫu `.env`, file mật khẩu `fed_enroll`, `README`, `SHA256SUMS`).
- Cấp mã kích hoạt cho 10.2.0.94 (`ceph-ai-federation create-code "10.2.0.94" --days 7` trên node SQL).
- Kiểm tra số liệu đến `inbox.c_<mã khách>` sau pha 5.

## 5. Nhật ký

| Ngày | Việc | Kết quả |
|---|---|---|
| 15/10/2026 | Lập kế hoạch | Chờ pha 1 trên 10.2.0.94 |
