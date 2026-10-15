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

## 2b. Kết quả pha 1 (15/10/2026) và quyết định

Báo cáo của agent trên 10.2.0.94 (`lab-hapu-ctl`, Ubuntu 22.04.5, 40 CPU, 62 GiB RAM, `/` còn 38 GiB):
- Bản cũ chạy **trực tiếp bằng Python/uvicorn** (`ceph-ai-dashboard.service`, thư mục `/opt/ceph-ai`,
  commit `fd09f3e` ngày 05/09/2026, chậm 646 commit). Có 3 file code sửa tại chỗ và file chưa theo dõi.
  **Không có podman/docker.** Chỉ Dashboard và Cinder backup alert chạy; `ai-pricing` và
  `nightly-ai-improvement` đang failed; không có Watcher/Worker. Bản cũ thực tế chỉ còn chạy một phần.
- Cấu hình ở `/opt/ceph-ai/.env`. `DATABASE_URL` trỏ tới **PostgreSQL ngoài máy, cổng 31081, hiện không
  kết nối được**. Rất có thể DB này nằm trên server PostgreSQL managed cũ, đã bị xoá ngày 12/10/2026;
  operator cần xác nhận.
- RabbitMQ 3.9.27 cài bằng apt, chạy tại chỗ, cổng 5672.
- Một cụm Ceph đọc được qua CLI: MON `hapu-ceph-01`, 17 OSD up/in, `HEALTH_WARN` do daemon khác phiên bản.
- Mạng đi ra: github.com, ghcr.io, pkg-containers.githubusercontent.com, 45.124.93.83:1207 đều OK.

**Quyết định:** không nâng cấp đè lên bản cũ. **Cài mới song song** theo mô hình production (container theo
digest, PostgreSQL riêng tại chỗ). Bản cũ giữ nguyên làm đường lùi. Dữ liệu DB cũ không mang sang (không truy
cập được). Ceph AI mới học lại từ đầu trên cụm `hapu`.

## 3. Các pha

### Pha 1 — Khảo sát (chỉ đọc) → báo cáo cho operator
- [x] Hệ điều hành, CPU/RAM/đĩa trống (`df -h`, chú ý thư mục chứa image podman/docker và `/var/lib/ceph-ai`).
- [x] Ceph AI đang chạy thế nào:
  - `podman ps -a` hoặc `docker ps -a`; tên container, image, tag/digest, thời gian chạy;
  - có checkout git không (`git -C <dir> log -1`, `git status --short`), ở thư mục nào;
  - unit systemd liên quan (`systemctl list-units | grep -i ceph-ai`);
  - file cấu hình: `/var/lib/ceph-ai/config/.env` hoặc `.env` trong checkout. Chỉ liệt kê **tên khoá**,
    không in giá trị.
- [x] DB: PostgreSQL ngoài, PostgreSQL trong container hay SQLite (`/var/lib/ceph-ai/ceph-ai.db`)? Ghi phiên
  bản PostgreSQL, kích thước DB, revision Alembic hiện tại (`alembic_version`).
- [x] RabbitMQ: chạy ở đâu, phiên bản.
- [x] Ceph AI đang giám sát những cụm nào (số cụm, `ceph -s` chỉ đọc). Có action nào đang
  `PENDING_APPROVAL`/`EXECUTING` không (nếu có thì không cập nhật lúc này).
- [x] Kết nối mạng đi ra:

  ```sh
  for t in github.com:443 ghcr.io:443 pkg-containers.githubusercontent.com:443 45.124.93.83:1207; do
    timeout 5 bash -c "</dev/tcp/${t%:*}/${t#*:}" && echo "$t OK" || echo "$t FAIL"; done
  ```
- [x] Viết báo cáo khảo sát vào `docs/operations/10.2.0.94-survey-<ngày>.md` hoặc gửi operator. Kết luận
  chọn một trong hai đường:
  - **Đường A (online)**: ra được `github.com` + `ghcr.io` → cập nhật theo
    `docs/immutable-production-release.md`.
  - **Đường B (offline)**: không ra được → chờ operator mang **bộ cài offline** (R8.1) sang.

### Pha 2 — Giữ lại bản cũ và chuẩn bị máy (cần operator đồng ý cho việc cài gói)
- [ ] **Cổng quyết định:** operator xác nhận DB cổng 31081 không còn, hoặc không cần dữ liệu trong đó. Nếu
  operator muốn giữ dữ liệu cũ thì dừng lại; phải khôi phục DB trước, plan này không làm phần đó.
- [ ] Lưu lại bản cũ vào `/root/ceph-ai-old-<ngày>/` (quyền 0700), ghi `sha256sum` từng file:
  - `git -C /opt/ceph-ai diff > local-changes.patch`, kèm danh sách file chưa theo dõi
    (`git status --short`);
  - `tar` toàn bộ `/opt/ceph-ai` (gồm `.env`) và các unit `ceph-ai-*` trong `/etc/systemd/system`.
  Không sửa và không xoá gì trong `/opt/ceph-ai`.
- [ ] Cài container runtime: `podman` và `podman-compose` 1.0.6 (`pip install podman-compose==1.0.6` trong
  venv riêng, hoặc gói apt nếu đúng bản). Ghi phiên bản podman. Ubuntu 22.04 có podman 3.4, cũ hơn bản đang
  chạy ở máy chính (4.9). Lỗi nào do bản podman thì ghi lại, không tự vá code Ceph AI.
- [ ] PostgreSQL tại chỗ: container `docker.io/library/postgres:18`, chỉ nghe `127.0.0.1:5432`, dữ liệu ở
  `/var/lib/ceph-ai-postgres` (0700).
  - Tạo user `ceph_ai` và DB `ceph_ai`.
  - Mật khẩu sinh ngẫu nhiên (`openssl rand -hex 24`), lưu ở `/root/.config/ceph-ai/db.pass`, quyền 0600.
  - Kiểm tra bằng `psql` qua `127.0.0.1`.
- [ ] RabbitMQ: dùng RabbitMQ 3.9.27 đang có, tạo vhost `ceph-ai` và user riêng (mật khẩu ngẫu nhiên, 0600).
  Không đổi user và vhost cũ. Nếu bản mới không chạy được với 3.9, ghi lỗi và báo operator; phương án dự phòng
  là container `rabbitmq:3.13-management` ở cổng khác.
- [ ] Sao lưu DB tự động: một timer systemd chạy `pg_dump -Fc` hằng đêm vào `/var/backups/ceph-ai`, giữ 7
  bản. Có thể làm sau pha 3, nhưng trước pha 6.

### Pha 3 — Cài bản mới song song (đường A, online)
Phiên bản đích do operator chỉ định: một commit `main` đã qua CI và digest
`ghcr.io/phamxuankhiem/ceph-ai@sha256:…` lấy từ artifact `registry-image-ref.txt` của lần CI đó.
- [ ] Clone sạch repo vào `/root/ceph-ai` (không dùng `/opt/ceph-ai`), checkout đúng sha đích.
- [ ] `podman login ghcr.io` bằng token `read:packages` do operator cấp. Lệnh cần TTY thật, nên operator
  tự gõ.
- [ ] Tạo `/var/lib/ceph-ai/config/.env` (0600) từ `.env.example` của **bản mới**. Chỉ chép từ `.env` cũ
  những khoá **có trong `.env.example` mới**, vì Settings dùng `extra="forbid"`:
  - `DATABASE_URL` / `RABBITMQ_URL` trỏ tới DB và vhost mới ở pha 2;
  - thông tin cụm Ceph (`CLUSTER_NAME`, `CEPH_MON_*`, cách chạy lệnh ceph, đường keyring);
  - Telegram (nếu có);
  - `AUTOPILOT_ENABLED=false`;
  - session secret ngẫu nhiên ≥ 32 byte;
  - mật khẩu admin Dashboard mới (không dùng mặc định).
- [ ] **Dừng bản cũ khi operator cho phép:** `systemctl stop ceph-ai-dashboard.service` (chưa `disable`), vì
  bản mới dùng cùng cổng Dashboard. Ghi giờ dừng.
- [ ] Chạy migration rồi khởi động theo `docs/immutable-production-release.md`:
  `deploy_preflight.sh` → `restart_container_stack.sh`, với `CEPH_AI_IMAGE=<digest>` và
  `DEPLOY_REF=<sha 40 ký tự>`.
  - Gặp preflight chỉ hợp với máy chính (ví dụ đường dẫn, tài khoản Codex, Vault): ghi lại, hỏi operator.
    Không tắt kiểm tra bằng biến `ALLOW_*`.
- [ ] Có lỗi không khắc phục được trong 30 phút → **đường lùi**:
  1. dừng stack mới;
  2. `systemctl start ceph-ai-dashboard.service`;
  3. báo operator.

**Kiểm tra sau khi cài**
- [ ] Mọi container `healthy`; không container nào restart liên tục.
- [ ] Đăng nhập Dashboard bằng mật khẩu admin mới. Cụm `hapu` hiện đúng: 17 OSD, cảnh báo phiên bản daemon.
- [ ] Watcher tạo hoặc cập nhật trạng thái cụm trong 10 phút; queue RabbitMQ được tiêu thụ (không dồn).
- [ ] Autopilot đang tắt. Không có action nào tự chạy.
- [ ] Sau 24 giờ ổn định, operator quyết định `systemctl disable ceph-ai-dashboard.service` cho bản cũ.
  Thư mục `/opt/ceph-ai` và bản lưu ở pha 2 vẫn giữ lại.

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
| 15/10/2026 | Pha 1 trên 10.2.0.94 | Bản cũ systemd/Python, DB cổng 31081 mất kết nối, không có podman; quyết định cài mới song song (mục 2b) |
