# Kế hoạch triển khai Impact Analysis và kiểm thử hồi quy theo kiến trúc

**Phạm vi đích:** repo `/root/ceph-ai` trên `10.3.55.213`  
**Trạng thái:** Đang triển khai theo từng lát; Pha 0 chưa hoàn thành
**Kế hoạch tổng:** [`Plan/regression-test-gate-plan.md`](../regression-test-gate-plan.md), Pha 4  
**Mục tiêu:** khi code thay đổi, xác định có căn cứ thành phần/luồng bị ảnh hưởng, chọn và chạy đúng test an toàn, giải thích được vì sao test được chọn; nếu không đủ tin cậy thì chạy full non-live suite.

## 1. Quyết định kiến trúc

- `tests/architecture/graph.yaml` là nguồn chuẩn máy đọc được cho source → component → flow → test; không duy trì impact map thứ hai.
- Trang Stream hiện tại là giao diện tra cứu thủ công: xem node, luồng, quan hệ và test đã khai báo. Nó **không** đọc Git diff, tính impact, chọn test hay chạy test.
- `scripts/validate_architecture_graph.py` hiện chỉ kiểm tra schema, tham chiếu node/flow, source/test path và Mermaid flow marker. Không được mô tả validator này như impact selector.
- Impact selection và thực thi test thuộc công cụ CLI/CI. Không cho Dashboard production nhận shell command, khởi tạo pytest tùy ý hoặc chạy test trên server production.
- Stream có thể về sau hiển thị báo cáo CI đã tạo và xác thực, ở chế độ chỉ đọc; không phải nguồn quyết định pass/fail và không thay CI.
- Mọi lựa chọn phải fail-safe: graph lỗi, path không map, node critical không có test, dependency chưa rõ hoặc report không khớp SHA ⇒ chọn full non-live suite hoặc đánh dấu gate thất bại; không được âm thầm giảm coverage.
- Mặc định không truy cập Ceph, RGW/S3, OpenStack, Telegram, AI provider hay hệ thống thật. Không chạy test `live`/`destructive` trong PR gate.

### Tiến độ triển khai — 2026-09-29

- [x] Baseline trên server: strict graph validation đạt với 70 node, 124 cạnh, 15 flow; nhóm test architecture/profile/Stream đạt 15 passed.
- [x] Slice Pha 0 về tính toàn vẹn schema: validator phát hiện cạnh trùng, giá trị cạnh sai kiểu, ID trùng của coverage rule/deployment variant, `require_checks` sai kiểu/rỗng và test path khai báo trong coverage rule nhưng không tồn tại.
- [x] Test validator sau lần mở rộng đầu: 9 passed; strict graph/path audit vẫn đạt.
- [x] Review fix: source/test path tuyệt đối, parent traversal, Windows path và symlink thoát repo bị từ chối; YAML loader hỗ trợ merge `<<` chuẩn và vẫn chặn duplicate explicit keys.
- [x] Kiểm tra sau review fix: 12 test validator passed; strict graph/path audit đạt.
- [x] Sửa tương thích YAML merge key `<<`: inherited mappings được merge theo thứ tự chuẩn, explicit key được override hợp lệ, duplicate explicit key vẫn bị từ chối; regression suite sau sửa đạt 25 passed.
- [x] Slice semantics cạnh: mọi `edge_kind` có chính sách impact tường minh; mặc định bảo thủ `both`, còn `not_wired` buộc `full_suite`. Graph schema được nâng lên v2 và validator từ chối loại cạnh thiếu/chưa biết policy.
- [x] Slice metadata node rủi ro cao: thêm `node_reviews` cho auth, cluster scope, action lifecycle, policy, executor và audit; ghi code-area owner, confidence, path/symbol/claim evidence; strict audit xác minh evidence tồn tại và nằm trong source mapping của node. Graph schema lên v3.
- [x] Validator test review metadata, file evidence và recursive source glob `**`; kiểm tra nhóm architecture/profile/Stream đạt 28 passed, strict audit đạt.
- [x] Tiếp tục review evidence cho persistence, cluster registry, durable outboxes, RabbitMQ incident queue và action-state event; tổng cộng 12/70 node có review record mức confidence cao. Claims chỉ nêu hành vi thấy trực tiếp trong các symbol được dẫn.
- [x] Review thêm request observability, Ceph query cache và Object Storage cache; evidence phân biệt rõ Ceph cache có snapshot disk/pruning còn Object Storage cache hiện chỉ process-local. Tổng cộng 15/70 node đã có review record.
- [x] Xác minh lát cache/observability: strict path audit đạt; nhóm test cache, rate limit, dashboard status, redaction, architecture/profile và Stream đạt 87 passed.
- [x] Cải tiến code Pha 0: validator kiểm tra định dạng `validation`, bắt buộc mỗi lệnh validation của node được coverage rule yêu cầu, báo cáo coverage gap và có `--strict-coverage` để fail nếu node critical/high không có test/validation. Gap hiện tại được nêu rõ là `dashboard.ai_tasks_unmounted` (medium), không bị che hoặc gán test giả.
- [x] Kiểm tra validator mới: 22 test validator và 9 test architecture profile/Stream passed; strict path + strict coverage validation đạt và liệt kê gap medium hiện hữu.
- [x] Thêm `edge_reviews` và validator kiểm tra cạnh review phải tồn tại trong graph, không trùng, có owner/confidence/evidence; strict path audit buộc evidence thuộc source mapping của ít nhất một endpoint. Báo cáo số cạnh đã/chưa review mà không giả vờ đã audit hết.
- [x] Review evidence lô đầu cho 6 cạnh của cluster registry, Watcher publisher, RabbitMQ transport/consumer và durable outbox delivery; validator suite đạt 25 passed, strict path + strict coverage đạt.
- [ ] Pha 0 chưa hoàn thành: 15/70 node và 6/124 cạnh đã review; còn 118 cạnh chưa review, chưa audit thủ công toàn bộ source mappings/coverage rules. Quy tắc `both` vẫn bảo thủ và chưa tối ưu theo từng cạnh. Node/cạnh thiếu review phải được xem là chưa xác minh; selector sau này phải fallback khi impact chạm vùng đó.

## 2. Luồng đích

```text
Git base/head diff
        ↓
Changed paths (bao gồm rename/delete)
        ↓
Graph path matcher → changed nodes
        ↓
Hướng lan truyền theo loại cạnh + shared coverage rules
        ↓
Affected components / flows / contracts
        ↓
Test selection + lý do truy nguyên
        ↓
Safety validation → selected non-live tests hoặc full-suite fallback
        ↓
CI report gắn base SHA, head SHA, graph revision và kết quả
        ↓
(Tuỳ chọn sau) Stream hiển thị report CI đã xác thực, chỉ đọc
```

Không suy ra dependency chỉ từ tên thư mục. Mỗi mapping/edge quan trọng phải có source evidence, loại quan hệ, owner, criticality và confidence/review status. Các quan hệ chưa review được đánh dấu chưa xác minh và kích hoạt fallback tương ứng.

## 3. Các giai đoạn triển khai

### Pha 0 — Chốt schema graph phục vụ impact

- [ ] Rà soát 70 node, 124 edge, 15 flow đã có; xác nhận con số hiện tại bằng strict validation trước khi bắt đầu implementation.
- [ ] Chuẩn hóa `source` thành path/glob có quy tắc rõ; bổ sung mapping riêng cho file cấu hình, migration, workflow CI, script build và generated asset khi cần.
- [ ] Bổ sung metadata tối thiểu cho node/edge: `owner`, `criticality`, `confidence` hoặc `reviewed`, `evidence` (file/symbol/route/event/API/schema); không lưu secret hay cấu hình triển khai nhạy cảm.
- [ ] Chuẩn hóa chiều dependency và ý nghĩa cạnh: producer/consumer, caller/callee, renders/serves, reads/writes, permission/scope boundary; ghi rõ cạnh nào được đi xuôi, ngược hoặc hai chiều khi tính impact.
- [ ] Khai báo test như entity có thể truy nguyên hoặc duy trì danh sách `tests` hiện có nhưng thống nhất contract; phân loại `unit`, `api`, `integration`, `security`, `browser`, `slow`, `live`, `destructive` và command label an toàn.
- [ ] Hoàn thiện `coverage_rules`: trigger node/edge/kind/path, flows bổ sung, test bắt buộc, mức fallback và lý do; loại bỏ rule hiện tại chưa được validator kiểm tra như `require_checks` nếu schema/validator chưa hỗ trợ.
- [ ] Đánh dấu coverage gap thật; không tạo mapping test giả chỉ để graph xanh. React node hiện `tests: []` cần có build/browser validation khai báo rõ và được selector hiểu.
- [ ] Nâng validator kiểm tra metadata bắt buộc, duplicate IDs/edges, test file tồn tại, rule fields, flow membership, path glob overlap/collision và mapping node active chưa có test/validation.
- [ ] Thêm version/schema migration policy để thay graph không làm report cũ bị diễn giải sai.

**Acceptance:** strict validation phát hiện graph sai cấu trúc, source/test hỏng, rule không hợp lệ và coverage gap theo mức criticality; report nêu được các node còn chưa được review.

### Pha 1 — Xây CLI xác định thay đổi từ Git

- [ ] Tạo `scripts/test_impact.py` với input tường minh `--base <sha>` và `--head <sha>`; CI phải truyền đúng base/head, không dựa vào branch name suy đoán.
- [ ] Chạy `git diff --name-status -z --find-renames <base> <head>` bằng subprocess argv (không shell), timeout hữu hạn, giới hạn output và kiểm tra SHA tồn tại.
- [ ] Xử lý add/modify/delete/rename/copy, path có khoảng trắng/newline, empty diff, shallow checkout và merge-base không tồn tại; thiếu lịch sử cần báo lỗi có hướng dẫn checkout sâu hơn hoặc fallback.
- [ ] Chỉ dùng changed paths đã normalize và nằm trong repo; từ chối path traversal/absolute path. Không lấy nội dung diff làm command hoặc YAML instruction.
- [ ] Hỗ trợ chế độ `--paths-file` cho unit test/local tooling, với cùng parser path và không tự chạy test.
- [ ] Xác nhận deterministic: cùng base/head/graph cho cùng tập changed path và report selection.

**Acceptance:** test Git fixture xác minh status/rename/delete/empty/shallow/path edge cases; CLI không gọi bất kỳ external service nào.

### Pha 2 — Thuật toán impact selection

- [ ] Ánh xạ changed path tới toàn bộ source patterns phù hợp; trả rõ matched node, pattern nào khớp và các path chưa map.
- [ ] Phân biệt file source với generated output/vendor/lockfile; mặc định không bỏ qua path lạ chỉ vì có vẻ generated. Quy tắc ignore phải được review, kiểm thử và hiển thị trong report.
- [ ] Bắt đầu traversal từ changed nodes, áp hướng cạnh theo edge kind, tìm consumer/downstream và các boundary upstream cần kiểm chứng contract.
- [ ] Mở rộng affected flows từ membership graph; áp coverage rule có điều kiện với logic xác định, không dùng rule mơ hồ hoặc thứ tự YAML làm kết quả khác nhau.
- [ ] Chọn test từ node, flow, rule và contract liên quan; dedupe nhưng giữ nhiều đường lý do để chứng minh test bảo vệ các phần nào.
- [ ] Shared/critical boundaries (auth, RBAC, cluster scope, DB model/migration, policy/approval, audit, event/schema, shared cache/client) bắt buộc mở rộng sang consumers đã khai báo và các test bắt buộc; nếu graph không chứng minh đầy đủ thì full non-live suite.
- [ ] Chu kỳ trong graph phải kết thúc an toàn; giới hạn kích thước/độ sâu và số node/edge để chống graph quá lớn hoặc lỗi cấu hình.
- [ ] Quy tắc fallback tối thiểu: unknown path; không node match; graph parse/schema lỗi; edge/flow chưa review; node critical không có test; test path thiếu; thay đổi manifest/selector/CI gate; path rename không map; hoặc selector lỗi ⇒ full non-live suite, hoặc fail gate nếu không thể chạy an toàn.
- [ ] Không cho `coverage_rules` loại bỏ test đã chọn bởi node/flow; rule chỉ được bổ sung/mở rộng phạm vi trừ khi có ngoại lệ reviewed rõ ràng.

**Acceptance:** unit test chứng minh producer change bao phủ consumer, consumer change bao phủ contract/caller, shared node mở rộng hợp lý, không bỏ test trùng lặp gây mất lý do, và mọi unknown/failure đều fallback đúng.

### Pha 3 — Report truy nguyên và giao diện dòng lệnh

- [ ] Tạo report JSON phiên bản hóa cùng Markdown dễ đọc; có base/head SHA, graph hash, thời điểm, changed paths/status, mapped/unmapped paths, changed nodes, affected nodes/flows, tests/checks, fallback reason và confidence.
- [ ] Với từng test, xuất ít nhất một đường giải thích, ví dụ `shared.cluster_scope → volumes API → Block Storage flow → tests/test_dashboard_volumes.py`.
- [ ] Phân biệt `selected`, `passed`, `failed`, `skipped`, `flaky`, `not_run`; test skipped/not_run không được tính là pass.
- [ ] Report không chứa diff, credentials, token, cookie, user data hoặc env values; sanitize ANSI/control chars và giới hạn kích thước/log.
- [ ] CLI mặc định chỉ phân tích và in selection (`--dry-run`); output ổn định cho CI và exit code khác nhau cho selector error, fallback, test failure.
- [ ] Ghi log có correlation/run ID và giữ report artifact theo retention CI đã cấu hình; không tạo log dài hạn không giới hạn trên server.

**Acceptance:** JSON schema test, deterministic snapshot test và test redaction; report xác minh được chính xác SHA và graph version đã dùng.

### Pha 4 — Chạy test an toàn, giới hạn tài nguyên

- [ ] Chỉ chạy test IDs/commands từ allowlist trong code/config đã review; không `shell=True`, không thực thi command do diff/manifest PR cung cấp tùy ý.
- [ ] Dùng pytest node IDs/file paths làm argv; command frontend build/check định nghĩa trong allowlist CI. Chặn markers `live`, `destructive` ở gate mặc định.
- [ ] Thêm timeout cho selector và từng suite, concurrency cap, output cap, cleanup process group khi timeout/cancel.
- [ ] Thực thi trong isolated CI runner/container với quyền tối thiểu, workspace sạch theo SHA, network deny-by-default hoặc egress allowlist cho dependency mirror cần thiết.
- [ ] Không chạy test trên Dashboard/production server. Nếu một test yêu cầu service ephemeral, runner dựng service cô lập và hủy sau job.
- [ ] Khi full non-live suite fallback, báo rõ lý do, command profile và số lượng test; không lặng lẽ thay bằng subset.
- [ ] Chỉ hỗ trợ retry cho test được phân loại flaky; giữ lần chạy đầu trong báo cáo, retry không xóa fail ban đầu.

**Acceptance:** test timeout/cancel xác nhận tiến trình con được dọn; kiểm chứng không có SSH/Ceph thật, credential production hay mutation ngoài môi trường test.

### Pha 5 — Unit, fuzz và đối chiếu selector

- [ ] Unit test path matching: exact/glob/overlap, duplicate pattern, rename/delete, generated paths, unknown path, traversal, case sensitivity.
- [ ] Unit test traversal: directed edge kinds, mixed direction, cycles, fan-out, fan-in, shared component, multi-flow, conditional config rules.
- [ ] Unit test selection: dedupe, critical/shared fallback, node/flow tests, empty tests, missing test, graph schema drift, report reasons.
- [ ] Property/fuzz test manifest lỗi: YAML malformed, duplicate IDs, wrong types, rất nhiều node/edge, edge tham chiếu node không tồn tại và input path bất thường.
- [ ] Tạo fixture graph nhỏ có expected impacts được review độc lập; không chỉ dùng graph production lớn để kiểm tra thuật toán.
- [ ] Đối chiếu selectors với full non-live suite trên thay đổi lịch sử đại diện (auth, scope, model, event, cache, UI, route riêng, config, migration); ghi trường hợp subset thiếu test và sửa graph trước khi bật gate.
- [ ] Thực hiện shadow mode trong CI: tạo report impact nhưng vẫn chạy full non-live suite; so sánh thời gian và coverage/miss risk trước khi subset được phép làm gate.
- [ ] Mutation test selector/rules ở các nhánh critical để chứng minh tests phát hiện edge bị đảo/bỏ và rule không được áp dụng.

**Acceptance:** không có known false-negative trong corpus review; unknown luôn fallback; shadow report được duyệt bởi maintainers trước khi subset gate được bật.

### Pha 6 — CI tích hợp theo mức rủi ro

- [ ] Thêm job `architecture-validate` chạy strict validator, unit tests selector, Python compile và frontend build theo runtime Node/Python đã pin.
- [ ] PR mode đầu tiên chạy impact selector ở shadow mode và full non-live suite; lưu report/artifact gắn commit SHA.
- [ ] Sau giai đoạn shadow được chấp thuận, chỉ cho phép subset thay full suite ở low-risk changes đã map đầy đủ; shared/critical/unknown vẫn chạy full suite.
- [ ] Chạy đầy đủ browser smoke khi React/Jinja/template/JS/CSS/navigation/API contract bị ảnh hưởng; selector phải map UI check vào report, không giả định Vite build thay thế browser test.
- [ ] Với thay đổi graph/selector/coverage rule/CI workflow, luôn chạy selector unit suite và full non-live suite.
- [ ] Dùng permissions GitHub tối thiểu; không cấp production secrets cho pull request từ fork hoặc job kiểm thử code không tin cậy.
- [ ] Gate release/deploy phải kiểm tra tested SHA == artifact/deploy SHA; test thất bại hoặc report thiếu/mismatch thì chặn promotion.

**Acceptance:** PR có report dễ đọc, đường ảnh hưởng và artifact test; full fallback hoạt động; không có secret/effect ra ngoài; deploy gate xác minh SHA chính xác.

### Pha 7 — Stream integration chỉ đọc (sau khi CLI/CI ổn định)

- [ ] Giữ khả năng hiện tại: node detail liệt kê flow, test, relationship; không tuyên bố danh sách này là selection đã chạy.
- [ ] Định nghĩa report contract/API chỉ đọc, gồm SHA, trạng thái, affected flow/test, fallback, kết quả và URL artifact CI.
- [ ] Hiển thị report khi SHA khớp checkout/PR context; stale/missing report có nhãn rõ, không trình bày là kết quả mới.
- [ ] Chỉ admin được xem nếu giữ route hiện tại; không tăng quyền hay làm lộ path/metadata nhạy cảm cho viewer.
- [ ] Không thêm nút “Chạy test” gọi shell/pytest trên server production. Nếu sau này cần dispatch CI, dùng GitHub workflow dispatch có allowlist, auth/service identity giới hạn, CSRF/audit/rate-limit, branch/SHA validation và approval riêng; tách thành plan/security review.
- [ ] Giữ Stream config profile secret-free, không probe external systems chỉ để dựng impact report.

**Acceptance:** UI chỉ trình bày report đã tạo ở CI và phân biệt trạng thái stale/not-run; không có thực thi code tùy ý từ web.

## 4. Kiểm thử bắt buộc của chính Impact Analysis

- Changed producer → chọn các consumer/downstream tests.
- Changed consumer → chọn API/producer contract tests khi quan hệ đã khai báo.
- Thay đổi `dashboard.auth`, `dashboard.cluster_scope`, shared model/migration, policy/approval/audit, event schema, cache/client → mở rộng sang flows/consumers bắt buộc hoặc full suite.
- Một node tham gia nhiều flow → union tests, không bỏ flow nào.
- Unknown/new file hoặc source glob không match → full-suite fallback.
- Delete/rename file và thay source pattern → tìm node cũ/mới hoặc fallback.
- Node critical không có test/validation → full suite và phát coverage gap.
- Manifest hỏng, rule không hợp lệ, test file bị xóa, Mermaid/manifest drift → fail strict validation và không trả selection hẹp.
- Empty diff → report no-op; CI vẫn chạy các gate repository bắt buộc.
- Timeout, cancellation, oversized diff/graph, malformed YAML/path → kết thúc hữu hạn, không process mồ côi.
- Nhiều cluster/deployment variant trong graph → selection không phụ thuộc dữ liệu/secret của installation; config variant chỉ mở rộng bộ test offline tương ứng.
- Selector báo selected tests nhưng runner skipped/not_run/fail → report đúng trạng thái, gate không pass.

## 5. Ma trận ưu tiên

| Mức | Phạm vi | Điều kiện |
|---|---|---|
| P0 | Graph schema/evidence, validator, Git diff parser, path-to-node mapping, directed traversal, test selection, fallback, report và unit tests | Bắt buộc trước khi dùng selection làm gate |
| P1 | Safe runner, CI shadow mode, historical diff corpus, coverage-gap handling, resource limits | Bắt buộc trước subset gating |
| P2 | CI artifact browser report, Stream read-only integration, richer ownership/coverage dashboard | Sau khi selector ổn định; không cản CLI/CI |
| Ngoài phạm vi hiện tại | Live Ceph acceptance, dữ liệu production, performance/DR rehearsal, mutation thật | Để giai đoạn riêng có môi trường được chỉ định |

## 6. Thứ tự rollout và rollback

1. Xác minh graph/CI baseline và đóng các lỗi strict validation.
2. Xây selector ở chế độ dry-run; chạy fixture và historical diff corpus.
3. Bật CI shadow: tạo report nhưng vẫn chạy full non-live suite.
4. Review false-negative risk và coverage gaps; chỉ maintainer approval mới chuyển low-risk subsets thành required checks.
5. Rollout subset gate theo loại thay đổi; shared/critical luôn giữ full fallback.
6. Nếu phát hiện miss, disable subset optimization ngay, quay lại full non-live suite, giữ report/artifact, thêm regression case và sửa graph/algorithm trước khi bật lại.

Không cần migration dữ liệu ứng dụng. Nếu report được lưu ngoài artifact CI, phải có TTL/retention bounded, giới hạn dung lượng, khóa theo repository/SHA và chính sách xóa; mặc định ưu tiên artifact retention hiện có.

## 7. Definition of Done

- [ ] CLI nhận base/head SHA, tự xác định diff an toàn, map paths → nodes → flows → tests và sinh report giải thích được.
- [ ] Mọi trường hợp unknown/critical gap/graph lỗi đều full-fallback hoặc fail closed; không có false-green do bỏ qua test.
- [ ] Runner chỉ chạy allowlisted non-live checks trong isolated CI, có timeout/concurrency/output/process cleanup.
- [ ] Selector được unit/property/fuzz-tested và đối chiếu với full suite ở shadow mode trên các thay đổi đại diện.
- [ ] CI report gắn tested SHA và graph hash; artifact/deploy SHA mismatch bị chặn.
- [ ] Stream (nếu triển khai P2) chỉ hiển thị report CI đã xác minh, có trạng thái stale và không thực thi command.
- [ ] Tài liệu nêu rõ giới hạn của graph thủ công, confidence và các coverage gap còn lại.
- [ ] Không dùng dữ liệu giả để tuyên bố đã nghiệm thu Ceph thật; các acceptance phụ thuộc môi trường thật tiếp tục được ghi riêng.

## 8. Bằng chứng baseline đã có — không tính là hoàn thành Impact Analysis

- Stream admin-only, read-only và secret-free; có thể xem relationships/flows/tests đã ánh xạ theo node.
- Graph hiện được strict-validate trong CI và có các test validator/profile/route.
- Strict validation/test checkpoint gần nhất được ghi ở `Plan/regression-test-gate-plan.md`.
- Những điều trên chưa chứng minh selector, safe runner, CI impact report hoặc regression selection tự động đã tồn tại.
