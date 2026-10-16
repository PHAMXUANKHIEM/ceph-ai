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
Bộ deploy của máy chính (`compose.yaml` ở gốc repo, `scripts/deploy/*`) **không dùng** trên 10.2.0.94, vì
nó cần unit, container `rabbitmq`, `.venv` và các mount chỉ máy chính có. Máy khách dùng **profile riêng**:
`deploy/customer/` và `scripts/customer/ceph-ai-customer.sh` (R8.1). Đọc `deploy/customer/README.md`.

- [x] **Cổng quyết định:** operator đã xác nhận DB cổng 31081 đã bị xoá, không cần giữ dữ liệu cũ
  (15/10/2026).
- [x] Đã lưu bản cũ vào `/root/ceph-ai-old-2026-10-15/` và kiểm tra checksum. Không sửa và không xoá gì
  trong `/opt/ceph-ai`.
- [ ] Cài gói. Đây là máy controller OpenStack; các gói dưới đây không đụng RabbitMQ, MariaDB hay dịch vụ
  OpenStack:
  `apt-get install -y podman golang-github-containernetworking-plugin-dnsname catatonit` (podman 3.4.4).
  Sau đó cài `podman-compose==1.0.6` vào venv riêng `/opt/ceph-ai-customer/venv`, vì APT không có gói này.
- [ ] Clone repo vào `/root/ceph-ai` và checkout **đúng sha đích** do operator đưa.
- [ ] `podman login ghcr.io`: operator tự gõ, vì lệnh cần TTY.
- [ ] Chạy `scripts/customer/ceph-ai-customer.sh preflight --image <digest> --dashboard-bind 0.0.0.0:8001`.
  Mọi mục phải PASS. Dùng **cổng 8001**, vì Dashboard cũ đang giữ `10.2.0.94:8000` và bản cũ chạy tiếp
  trong lúc thử.

### Pha 3 — Cài bản mới song song bằng profile khách
- [ ] `ceph-ai-customer.sh install`, với các tham số:
  - `--image <digest>`;
  - `--ssh-key <file do SSH_KEY_PATH cũ trỏ tới>`;
  - `--old-env /opt/ceph-ai/.env`;
  - `--dashboard-bind 0.0.0.0:8001`.

  Script tự làm các việc sau:
  - sinh mật khẩu, không in ra;
  - chép khoá SSH thành bản riêng, khoá gốc giữ nguyên;
  - dựng `.env` mới, chỉ lấy các khoá image nhận;
  - đặt `AUTOPILOT_ENABLED=false`;
  - khởi động PostgreSQL và RabbitMQ **trong stack**, không mở cổng ra ngoài;
  - chạy migration, khởi động 4 dịch vụ ứng dụng;
  - cài `ceph-ai-customer.service`;
  - kiểm tra sau khởi động.
- [ ] Gửi operator phần báo cáo `carried / replaced / dropped_unknown / dropped_on_purpose`. Phần này chỉ
  có tên khoá. Nếu khoá cần cho cụm Ceph bị bỏ (`dropped_unknown`), báo operator và không tự thêm vào.
- [ ] Gặp lỗi do podman 3.4 (mạng, healthcheck, `--init`): ghi nguyên văn lỗi rồi báo operator. Không sửa
  code Ceph AI, không bỏ qua kiểm tra.
- [ ] **Đường lùi:**
  1. `ceph-ai-customer.sh stop`;
  2. `systemctl disable ceph-ai-customer.service`.

  Bản cũ vẫn chạy ở cổng 8000, không bị đụng tới.

**Kiểm tra sau khi cài**
- [ ] `ceph-ai-customer.sh status`: 6 container `running`, không restart.
- [ ] Đăng nhập `http://10.2.0.94:8001` bằng mật khẩu trong `/root/.config/ceph-ai/dashboard-admin.pass`.
  Cụm `hapu` hiện đúng: 17 OSD, cảnh báo phiên bản daemon.
- [ ] Watcher cập nhật trạng thái cụm trong 10 phút; queue RabbitMQ được tiêu thụ.
- [ ] Autopilot đang tắt. Không action nào tự chạy.
- [ ] `ceph-ai-customer.sh backup` chạy được (có `.dump` và `.sha256` trong `/var/backups/ceph-ai`).
- [ ] Sau 24 giờ ổn định, operator mới quyết định có tắt bản cũ (`ceph-ai-dashboard.service`) và chuyển
  bản mới sang cổng 8000 hay không.

### Pha 4 — Gói tri thức (R8.2, khi bộ cài có)
- [ ] Nếu bộ cài có `knowledge-pack-<version>.json`: nhập theo `README`, rồi ghi số mục đã nhập.
- [ ] Xác nhận gói không làm thay đổi độ tin cậy Autopilot của cụm tại chỗ.

### Pha 5 — Kích hoạt federation (chỉ khi `45.124.93.83:1207` OK ở pha 1 và bản mới có `/federation`)
- [ ] Operator đưa **file mật khẩu `fed_enroll`** (có trong bộ cài, hoặc gửi riêng). Đặt file vào
  `/var/lib/ceph-ai-customer/config/federation-enroll.pass`, quyền 0600, chủ sở hữu uid 10001 (user của container).
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
- R8.1: profile cài cho máy khách (`deploy/customer/`, `scripts/customer/`) — đã có; còn đóng gói bộ cài offline (image `podman save` theo digest, `compose.yaml`, `install.sh`/`upgrade.sh`,
  mẫu `.env`, file mật khẩu `fed_enroll`, `README`, `SHA256SUMS`).
- Cấp mã kích hoạt cho 10.2.0.94 (`ceph-ai-federation create-code "10.2.0.94" --days 7` trên node SQL).
- Kiểm tra số liệu đến `inbox.c_<mã khách>` sau pha 5.

## 5. Nhật ký

| Ngày | Việc | Kết quả |
|---|---|---|
| 15/10/2026 | Lập kế hoạch | Chờ pha 1 trên 10.2.0.94 |
| 15/10/2026 | Pha 1 trên 10.2.0.94 | Bản cũ systemd/Python, DB cổng 31081 mất kết nối, không có podman; quyết định cài mới song song (mục 2b) |
| 15/10/2026 | Pha 2 dừng ở preflight của máy chính | Viết profile cài cho máy khách (R8.1); pha 2–3 dùng `scripts/customer/ceph-ai-customer.sh` |
| 16/10/2026 | Lần cài thứ 4 trên 10.2.0.94 trượt SMOKE | Bản cũ (chạy root) dùng chung `/var/lib/ceph-ai`, `/run/ceph-ai`; stack khách chuyển sang `/var/lib/ceph-ai-customer`, `/run/ceph-ai-customer` |
