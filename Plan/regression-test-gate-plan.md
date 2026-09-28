# Architecture-Aware Regression Test Gate Plan — Ceph-AI

## Mục tiêu

Mô tả kiến trúc và các luồng phụ thuộc của toàn bộ tool thành sơ đồ có thể tra
cứu bằng người và máy. Khi code thay đổi, dùng sơ đồ đó lần theo ảnh hưởng
trực tiếp và downstream để chọn test cần chạy; nếu không thể xác định an toàn,
chạy full non-live suite. Trước release chạy bộ hồi quy rộng hơn, kiểm tra
trình duyệt và tạo báo cáo truy nguyên theo commit.

Sơ đồ kiến trúc là nguồn chuẩn để trả lời ba câu hỏi: thay đổi chạm thành phần
nào, thành phần đó tham gia các luồng nào, và cần chạy test nào để bảo vệ các
luồng ấy. Sơ đồ phải mô tả quan hệ runtime/data/control thực tế, không chỉ
phân nhóm file theo tên thư mục.

Kế hoạch này tập trung vào code và kiểm thử tự động. Test mặc định không được
kết nối Ceph/OpenStack/RGW/Telegram thật hoặc phát sinh thao tác thay đổi dữ
liệu trên hệ thống thật. Các nghiệm thu cần dữ liệu/backend thật được ghi riêng
và chỉ thực hiện trong môi trường kiểm thử được chỉ định.

## Hiện trạng đã kiểm tra

- `pyproject.toml` khai báo pytest; cấu hình mặc định loại marker `live` bằng
  `-m 'not live'`.
- `.github/workflows/ci-cd.yml` chạy Python 3.11, cài package, build
  `ceph-health-dashboard` và chạy `pytest` trên pull request vào `main`.
- Workflow hiện deploy tự động sau khi test thành công trên push vào `main`;
  chưa có browser smoke/release artifact chuyên cho regression.
- Có nhiều test route/API theo từng module (`tests/test_dashboard_*.py`), test
  domain, worker, policy và security rải trong `tests/`.
- `tests/conftest.py` đã có `dashboard_client`, SQLite in-memory, tài khoản và
  cluster giả lập; cấu hình SSH/Telegram/AI được ghim để hạn chế phụ thuộc
  `.env` thật.
- React dashboard có lệnh `npm run build`; chưa thấy Playwright trong
  `ceph-health-dashboard/package.json`.

## Nguyên tắc và tiêu chí chung

1. Test phải kiểm tra hành vi và contract, không khóa chặt implementation nội
   bộ nếu không cần thiết.
2. Mỗi bug hồi quy được sửa phải có test tái hiện lỗi trước khi đóng issue.
3. Test dùng fake/mocks hoặc service ephemeral; không dùng cluster thật trong
   PR gate.
4. Test `live`, test chậm và test phá hủy phải có marker riêng, opt-in, có
   timeout và tài liệu điều kiện chạy.
5. Khi thay đổi API, quyền, cluster scope, approval, audit, cache hoặc schema,
   phải chạy nhóm test liên quan xuyên module.
6. Báo cáo phải gắn với commit SHA; phân biệt `passed`, `failed`, `skipped`,
   `flaky` và `not run`. Skip không được tính là pass.
7. Không tự hạ hoặc bỏ test để làm CI xanh. Thay đổi baseline cần giải thích,
   người chịu trách nhiệm và ngày xem xét lại.

## Pha 0 — Lập bản đồ kiến trúc và luồng hoạt động

- [ ] Mô tả system context: người dùng/operator, Dashboard, Watcher, Worker,
  message broker, database/cache, Ceph cluster, OpenStack/Cinder, RGW/S3,
  Telegram, AI provider và các tích hợp khác; thể hiện trust boundary và
  credential boundary.
- [ ] Vẽ component/runtime diagram cho Jinja/JavaScript, React/Vite bundle,
  FastAPI routes, shared services/models, Watcher collectors, Worker/executor,
  scheduler, event bus, DB/cache và external systems.
- [ ] Vẽ sequence diagram cho các luồng chính, tối thiểu:
  - Browser request → auth/RBAC → cluster selection/scope → API → cache/DB hoặc
    collector → response; gồm cache hit, cache miss/refresh, stale/partial và
    timeout/error.
  - Polling/event flow: Ceph → Watcher → snapshot/DB/event → API/WebSocket/SSE
    → dashboard/chat/log consumers.
  - Read-only query và background job; thể hiện nơi chạy blocking SSH/subprocess
    và ranh giới timeout/concurrency.
  - Mutation flow: UI/API → validation/preflight → Action preview → approval →
    queue/Worker → executor → post-check/reconciliation → audit/event/UI.
  - Chat/AI flow: UI → session/context policy → tool/diagnosis → evidence →
    proposal/approval nếu có; ghi rõ đường nào không được phép tự thực thi.
  - External integrations: Cinder/Nova/Glance, RGW/S3, Telegram, LDAP/Vault và
    AI provider, kèm timeout/fallback/cache và dữ liệu đi qua từng ranh giới.
- [ ] Vẽ dependency/data-flow diagram cho shared concern: authentication,
  authorization/capability, selected cluster propagation, audit, cache keys,
  event names, schemas/migrations, secret handling và frontend DOM/API
  contracts.
- [ ] Ghi failure paths cho mỗi luồng: dependency unavailable, timeout,
  partial response, stale snapshot, retry/circuit breaker, duplicate event/job,
  cancellation, permission denied và reconciliation mismatch.
- [ ] Tạo một graph manifest có ID ổn định cho `component`, `route`, `event`,
  `data_contract`, `external_dependency`, `permission_boundary` và `test`.
  Mỗi cạnh biểu diễn quan hệ có nghĩa, ví dụ `calls`, `publishes`, `consumes`,
  `reads`, `writes`, `requires_permission`, `renders`, `covered_by`.
- [ ] Mỗi node/edge ghi source evidence (file, route, event/API/schema), owner,
  criticality và confidence. Quan hệ suy ra tự động phải được phân biệt với
  quan hệ do người review xác nhận.
- [ ] Chốt schema manifest tối thiểu. Ví dụ minh họa:

  ```yaml
  nodes:
    dashboard.volumes_api:
      kind: route
      source: dashboard/routes/volumes.py
      criticality: high
    shared.cluster_scope:
      kind: component
      source: dashboard/cluster_scope.py
      criticality: critical
    ui.block_storage:
      kind: page
      source: dashboard/templates/block_storage.html
      criticality: high
    test.volumes_api:
      kind: test
      source: tests/test_dashboard_volumes.py
  edges:
    - [shared.cluster_scope, dashboard.volumes_api, requires_scope]
    - [dashboard.volumes_api, ui.block_storage, serves]
    - [dashboard.volumes_api, test.volumes_api, covered_by]
  ```

  IDs/edge names ở trên là ví dụ; pha kiểm kê phải xác minh lại theo code,
  không sao chép thành khai báo được coi là sự thật.
- [ ] Đặt sơ đồ đọc được dưới `docs/architecture/` bằng Mermaid; manifest máy
  đọc được đặt tại `tests/architecture/` hoặc vị trí tương đương. Mermaid và
  manifest phải tham chiếu cùng ID để phát hiện drift.
- [ ] Kiểm kê toàn bộ route/page, API, domain module, worker, quyền, cluster
  scope, cache/snapshot, mutation, approval/audit và test; đánh dấu node chưa có
  test thay vì giả định đã được bảo vệ.

**Hoàn tất pha:** reviewer có thể bắt đầu từ một route, event hoặc service và
lần theo upstream/downstream tới dữ liệu, quyền, external dependency và test;
các luồng quan trọng có sơ đồ sequence và đường lỗi; manifest hợp lệ và có
test kiểm tra liên kết với sơ đồ.

## Pha 1 — Baseline và kiểm kê test

- [ ] Chốt commit nền và lưu kết quả hiện tại của `pytest`, frontend build,
  thời gian chạy, test fail/skip/warning; không lấy số liệu worktree khác làm
  baseline.
- [ ] Tạo inventory có cấu trúc cho route/page, API, module domain, worker,
  permission, cluster scope, cache/snapshot, mutation, approval và audit.
- [ ] Ánh xạ từng nhóm tính năng tới test file hiện có; ghi rõ phần chưa có test.
- [ ] Tạo danh sách critical journeys:
  - Login, quyền và điều hướng; chọn cluster và cluster isolation.
  - Dashboard/health, nodes/metrics, pools/PGs/CRUSH.
  - Block Storage, trash/restore, approval/action/audit.
  - Object Storage buckets, S3 users/keys, RGW metrics.
  - Backup, restore drill, clusters lifecycle.
  - Alert Telegram, Log Intelligence, AI chat/task.
  - Vitastor flows riêng, không gộp nhầm với Ceph.
- [ ] Phân loại luồng `read-only`, `approval-gated mutation`, `destructive` và
  `external integration`.

**Hoàn tất pha:** inventory có owner/module, route/API, test liên quan và rủi
ro; mọi route quan trọng đều được ánh xạ hoặc có mục thiếu test.

## Pha 2 — Quy ước test và nền tảng chạy an toàn

- [ ] Chuẩn hóa pytest markers: `unit`, `api`, `integration`, `security`,
  `browser`, `slow`, `live`, `destructive` (chỉ bổ sung khi phù hợp với suite).
- [ ] Giữ mặc định loại `live` và `destructive`; CI PR chỉ chạy test cô lập.
- [ ] Rà soát fixture để bảo đảm không đọc secret thật, không sửa `.env`, không
  gửi Telegram/email, không gọi AI provider và không gọi SSH/Ceph ngoài mock.
- [ ] Thêm guard dùng trong test để phát hiện outbound network/SSH ngoài danh
  sách service ephemeral cho các test cần thiết.
- [ ] Chuẩn hóa timeout toàn cục và timeout riêng cho test subprocess/browser;
  test treo phải kết thúc có chẩn đoán, không chiếm runner vô hạn.
- [ ] Chuẩn hóa fixture cho trạng thái Ceph OK/WARN/ERR, node timeout, dữ liệu
  thiếu/malformed, cache stale, secondary/inactive cluster và viewer/operator/
  admin.
- [ ] Đảm bảo test mutation chỉ dùng database/test double; không thực thi lệnh
  quản trị lên cluster thật.

**Hoàn tất pha:** chạy test mặc định trong môi trường sạch không phụ thuộc
`.env`, network bên ngoài hoặc thứ tự test; các marker và quy tắc opt-in được
tài liệu hóa.

## Pha 3 — Contract và regression test theo luồng kiến trúc

- [ ] Lập contract cho API quan trọng: status/error shape, pagination, stale/
  refreshing metadata, cluster context, authentication và authorization.
- [ ] Bổ sung permission matrix cho anonymous, viewer, operator, admin; xác
  minh cả endpoint và hành động giao diện không vượt quyền.
- [ ] Bổ sung multi-cluster isolation cho dữ liệu DB/cache/job/event và truy vấn
  có cluster selector.
- [ ] Bổ sung regression matrix cho mutation: preview → approval → execute →
  post-check → audit; kiểm tra từ chối khi approval sai/hết hạn, target sai,
  evidence stale hoặc post-check thất bại.
- [ ] Kiểm tra API nhanh không gọi SSH/Ceph đồng bộ; cache hit/miss, lỗi backend,
  partial result và stale response có contract rõ.
- [ ] Thêm malformed/oversized input, pagination bounds, duplicate request,
  timeout và cancellation vào endpoint rủi ro cao.
- [ ] Mọi defect mới phải bổ sung test vào node/flow tương ứng và cập nhật
  manifest/sơ đồ nếu phát hiện dependency hoặc nhánh xử lý bị thiếu.
- [ ] Với mỗi critical journey, test các nhánh success, denied, timeout,
  partial/stale và recovery theo failure path đã mô tả trong sơ đồ.

**Ưu tiên test đầu tiên:** auth/RBAC, cluster scope, Action approval/audit,
Block Storage destructive flows, Object Storage key/bucket security, restore,
cache/snapshot và các trang có thay đổi gần đây.

## Pha 4 — Lan truyền ảnh hưởng và chọn test từ graph

- [ ] Dùng graph manifest kiến trúc làm nguồn chuẩn; không duy trì một impact
  map độc lập bị lệch khỏi sơ đồ.
- [ ] Viết `scripts/test_impact.py` nhận base/head SHA, ánh xạ changed paths tới
  graph nodes, rồi duyệt cạnh upstream/downstream có liên quan để tìm flows,
  contracts và test cần chạy.
- [ ] Phân biệt hướng lan truyền theo loại thay đổi: thay đổi producer cần test
  consumers; thay đổi consumer cần test caller/API contract; schema/event/auth/
  cluster-context thay đổi phải lan tới mọi writer/reader/guard tương ứng.
- [ ] Mỗi test được chọn phải có lời giải thích dạng đường đi, ví dụ:
  `shared.cluster_scope → volumes API → inventory cache → Block Storage page →
  tests/test_dashboard_volumes.py`.
- [ ] Thay đổi node/edge của graph, đổi tên/xóa file, unknown path, graph lỗi,
  Mermaid thiếu ID hoặc node critical không có test phải được xử lý rõ; nếu
  không thể chứng minh tập test đủ bao phủ thì chạy full non-live suite.
- [ ] Sinh impact report cho PR: changed nodes, affected flows, selected tests,
  unverified edges và fallback decision.
- [ ] Có unit tests cho graph traversal và selection: cạnh có hướng, nhiều
  consumer, chu kỳ, shared dependency, rename/delete, unknown path, empty diff,
  graph thiếu node và critical flow chưa có test.
- [ ] So sánh selection với full suite định kỳ; nếu phát hiện bỏ lọt consumer,
  sửa graph/algorithm và thêm regression case trước khi tiếp tục dựa vào
  selection.

**Hoàn tất pha:** với mỗi diff, CI giải thích đường ảnh hưởng từ code tới
luồng/tính năng/test; không có thay đổi production nào âm thầm rơi khỏi graph.

## Pha 5 — Browser smoke test và UI regression

- [ ] Chọn Playwright cho smoke test trình duyệt; cài browser/dependency theo
  lockfile và chạy trong môi trường CI cố định.
- [ ] Tạo tài khoản/cluster/dữ liệu giả lập qua fixture, không đăng nhập
  production.
- [ ] Viết smoke test cho các route/page trong critical journey: trang render,
  không có uncaught console error, API chính không 5xx, navigation và cluster
  context không mất.
- [ ] Bổ sung interaction test cho search/filter/pagination, modal open/close,
  empty/loading/error/stale states và responsive cơ bản.
- [ ] Với thao tác destructive, chỉ xác minh dialog/approval contract bằng
  fixture; không xác nhận mutation tới Ceph thật.
- [ ] Chụp screenshot và lưu console/network trace khi fail; artifact phải che
  token, cookie, query secret và dữ liệu nhạy cảm.
- [ ] Chạy browser test trên Chrome trước; thêm Firefox/WebKit chỉ khi runtime
  và thời gian CI đã ổn định.

**Hoàn tất pha:** bộ smoke chạy ổn định trên các trang critical, có artifact
khi lỗi và không có outbound call tới production.

## Pha 6 — CI gate theo tầng và báo cáo

- [ ] PR gate nhanh: format/lint/type check có sẵn, Python compile/import,
  frontend build và impact-selected unit/API/security tests.
- [ ] PR gate đầy đủ: full non-live pytest, browser smoke và contract tests cho
  thay đổi shared/high-risk.
- [ ] Tách job chạy song song khi không tranh chấp fixture/service; giữ thứ tự
  rõ cho migration, integration và browser test.
- [ ] Lưu JUnit XML, coverage XML, frontend build output, browser screenshot/
  trace và manifest commit SHA dưới một CI run.
- [ ] Báo cáo gồm pass/fail/skip/flaky/not-run, thời gian mỗi suite, impacted
  modules, API contracts, commit SHA và lý do test được chọn.
- [ ] Không cho deploy job chạy nếu gate bắt buộc thất bại; xác minh chính xác
  SHA được test trùng SHA được deploy.
- [ ] Quy định retry chỉ dành cho phân loại flaky đã nhận diện; lần retry
  thành công không được xóa kết quả fail đầu tiên khỏi báo cáo.

**Hoàn tất pha:** PR hiển thị regression report truy nguyên được và deploy
dependency chỉ phụ thuộc vào gate bắt buộc đạt.

## Pha 7 — Coverage, mutation testing và duy trì chất lượng

- [ ] Thu thập coverage baseline trên cùng commit/test command; ưu tiên branch
  coverage cho policy, auth, cluster scope, approval, reconciliation và
  destructive endpoints.
- [ ] Đặt ngưỡng ban đầu sau khi đo baseline; không áp ngưỡng tùy ý trước khi
  có số liệu đáng tin cậy.
- [ ] Gate coverage trên code mới hoặc critical modules; không để coverage tổng
  cao che module rủi ro thấp coverage.
- [ ] Đánh giá mutation testing cho các module policy/permission/action để
  phát hiện test chỉ chạy code nhưng không kiểm chứng kết quả.
- [ ] Theo dõi flaky test, runtime và failure ownership; sửa hoặc quarantine
  có thời hạn, không bỏ khỏi gate vô thời hạn.
- [ ] Mỗi thay đổi schema/API/event phải cập nhật contract và test consumer.

**Hoàn tất pha:** ngưỡng được hiệu chỉnh từ baseline, critical paths có test
assertion đủ mạnh và flaky có owner/thời hạn xử lý.

## Ma trận cổng theo thay đổi

| Loại thay đổi | Test bắt buộc |
| --- | --- |
| Helper/domain đơn lẻ | Unit test trực tiếp + impact tests |
| API route/template/JS cùng tính năng | Unit/API liên quan + browser smoke trang đó |
| Shared model/config/auth/cluster scope | Full non-live pytest + permission/isolation contracts |
| Cache/snapshot/event/polling | Cache/concurrency/stale/reconnect tests + consumers downstream |
| Action/Worker/approval/audit | Security/policy tests + preview/execute/post-check/audit integration |
| Database migration | Upgrade từ schema cũ, fresh install, downgrade/rollback nếu hỗ trợ |
| React source/Vite | TypeScript/Vite build + page smoke và interaction bị ảnh hưởng |
| Critical/destructive workflow | Full non-live suite + browser confirmation flow + isolated backend test |

## Các hạng mục nghiệm thu phụ thuộc dữ liệu thật — để sau

- [ ] Ceph live acceptance, performance benchmark và node failure rehearsal.
- [ ] OpenStack/Cinder, RGW, LDAP/Vault, Telegram và provider AI acceptance.
- [ ] Restore/DR drill có dữ liệu, đo RPO/RTO thực tế.
- [ ] Soak test dài hạn và xác minh log/metric production.

Các hạng mục này không được mô phỏng bằng dữ liệu giả rồi tuyên bố đã nghiệm
thu. Chúng cần môi trường và dữ liệu được chỉ định riêng.

## Thứ tự triển khai đề xuất

1. Pha 0: baseline và feature/test inventory.
2. Pha 1: baseline test và mapping test hiện có.
3. Pha 2: test isolation, fixture và marker an toàn.
4. Pha 3: contract, permission, cluster isolation và regression theo flow.
5. Pha 4: graph traversal và impact report theo diff.
6. Pha 5: Playwright smoke cho critical journeys.
7. Pha 6: CI gates và báo cáo truy nguyên theo SHA.
8. Pha 7: coverage/mutation quality và bảo trì suite.

## Definition of Done

- Sơ đồ kiến trúc, sequence flow, failure path và graph manifest phản ánh
  luồng runtime hiện tại; sơ đồ và manifest được kiểm tra drift trong CI.
- Mỗi thay đổi được lần theo graph tới các flow/consumer bị ảnh hưởng và nhóm
  test được chọn có lời giải thích cụ thể.
- Regression test trực tiếp và test downstream liên quan đều đạt.
- Shared/high-risk change chạy full non-live suite.
- Browser smoke và frontend build đạt khi UI bị ảnh hưởng.
- Không có test mặc định nào tác động Ceph thật hoặc gửi dữ liệu ra ngoài.
- Báo cáo phân biệt fail, skip, flaky và not-run, gắn đúng commit SHA.
- Các nghiệm thu cần backend/dữ liệu thật được đánh dấu chưa xác minh, không
  được tính là hoàn tất nhờ mock.
