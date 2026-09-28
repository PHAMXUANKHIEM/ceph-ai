# Sơ đồ kiến trúc Ceph-AI

Tài liệu này mô tả kiến trúc theo mã nguồn trong checkout được đọc khi tạo sơ
đồ. Đây là bản đồ để review thay đổi và chọn kiểm thử hồi quy; không phải cam
kết rằng mọi API đều dùng chung một collector hay mọi cập nhật đều realtime.

## Phạm vi và cách đọc

- Mã nguồn xác định các luồng: `dashboard/app.py`, `watcher/main.py`,
  `watcher/remediation_main.py`, `worker/main.py`, `shared/mq.py` và các router.
- Luồng Ceph và Vitastor là hai product context riêng. Chúng có thể dùng chung
  process infrastructure/DB nhưng có model, API, credentials và UI riêng.
- Một số API Dashboard truy vấn Ceph/RGW/OpenStack trực tiếp; một số dùng
  process cache hoặc dữ liệu đã lưu. Hãy xem từng API cụ thể trước khi kết
  luận độ trễ hay nguồn dữ liệu.
- Sơ đồ lõi không đồng nghĩa mọi cài đặt đều bật đủ tích hợp. Các cạnh có điều
  kiện phụ thuộc cấu hình được liệt kê ở phần cấu hình triển khai và trong
  `deployment_variants` của graph.
- Đường liền trong sơ đồ là quan hệ đã đọc thấy trong code; có thể chỉ chạy
  khi cấu hình bật. Đường nét đứt là code path có điều kiện cấu hình.
  `Not wired` ghi rõ một kết nối mà sơ đồ impact cần biết là hiện chưa có.
- Graph có ID tương ứng trong
  [`tests/architecture/graph.yaml`](../../tests/architecture/graph.yaml).

## 1. System context và thành phần runtime

```mermaid
flowchart LR
  operator[Operator / Admin]
  browser[Browser\nJinja + JavaScript\nReact 18 + Vite bundle]
  config[(Runtime settings\n+ cluster registry)]
  subgraph app[Ceph-AI application]
    dashboard[Dashboard\nFastAPI routes + session auth]
    domain[Shared domain\nRBAC / cluster scope / policy / audit]
    db[(Shared SQL database\nSQLAlchemy models)]
    cache[(Process-local caches\nCeph query / object storage)]
    watcher[Watcher\nhealth + telemetry collectors]
    remediation[Remediation Watcher\nhealth + post-action verification]
    broker[(RabbitMQ\nincidents + DLQ)]
    worker[Worker\nincident + delegated consumers\napproved-action poller + background loops]
    outbox[(Durable incident / Telegram outboxes)]
    directory[LDAP / AD validation adapter]
    scheduler[Backup scheduler\ninside Worker event loop]
    executor[Worker executors\nSSH / Ceph / Cinder / backup]
    vitawatcher[Vitastor monitor]
  end
  ceph[(Ceph clusters)]
  vita[(Vitastor clusters / etcd)]
  openstack[(OpenStack APIs)]
  rgw[(RGW / S3)]
  targets[(Backup targets\nSSH or S3)]
  ai[(AI provider / local app server)]
  identity[(OIDC / LDAP / AD provider)]
  vault[(Optional Vault secret backend)]
  telegram[(Telegram Bot API)]
  logs[(Node logs / Loki)]

  operator --> browser
  config --> dashboard
  config --> watcher
  config --> remediation
  config --> worker
  browser <-->|HTTP + signed session| dashboard
  browser <-->|WebSocket /ws/incidents| dashboard
  dashboard --> domain
  dashboard <--> db
  dashboard <--> cache
  dashboard -->|some routes query live| ceph
  dashboard -. configured Cinder .-> openstack
  dashboard -. configured RGW/S3 .-> rgw
  dashboard -. enabled AI provider .-> ai
  dashboard -. configured Telegram bot .-> telegram
  dashboard -->|log query routes| logs

  watcher -->|poll / collect| ceph
  watcher -->|persist health, metrics, incidents| db
  watcher -->|publish incident envelope| broker
  watcher -. enabled alert channel .-> telegram
  watcher -->|Vitastor monitoring thread| vitawatcher
  vitawatcher --> vita
  vitawatcher --> db
  remediation -->|health poll + verification| ceph
  remediation --> db
  remediation --> broker

  broker -->|incidents queue| worker
  worker <--> db
  worker <--> outbox
  worker -->|delegated task consumer| broker
  worker -. enabled diagnosis provider .-> ai
  worker --> executor
  scheduler -. coroutine in same Worker .-> worker
  scheduler --> db
  executor --> ceph
  executor --> openstack
  executor -. configured target .-> targets
  executor -. configured RGW .-> rgw
  worker -. enabled alert channel .-> telegram
  dashboard -->|LDAP/AD validation only| directory
  directory --> identity
  directory -.->|secret_ref=vault:...| vault
  worker -.->|OIDC role mapping reconciliation only| rgw

  classDef external fill:#f5f5f5,stroke:#777,color:#111;
  class ceph,vita,openstack,rgw,targets,ai,telegram,logs,identity,vault external;
```

Đường nét đứt biểu thị integration có điều kiện theo cấu hình. Worker thực tế
chạy nhiều loop độc lập (incident consumer, delegated-AI consumer,
approved-action poller, backup scheduler, bucket logging, RGW audit,
Telegram/incident outbox và heartbeat); không phải mọi loop đều cần cấu hình
external service tương ứng.

### Process ownership

| Process | Trách nhiệm quan sát được | Tài nguyên dùng chung |
| --- | --- | --- |
| Dashboard | HTML/API, auth/session, chat, CRUD và preview/approval; một số truy vấn read-only tới backend | DB; một số cache trong process; Telegram chat/approval listener chỉ khởi động khi `telegram_listener_enabled` |
| Watcher | Poll health và collectors; ghi heartbeat/metrics/incident; phát incident envelope; các loop theo cluster | DB, RabbitMQ, SSH/Ceph; Vitastor loop riêng được khởi chạy từ `run_all_clusters()` |
| Remediation Watcher | Poll health nhanh, reconcile Incident/Action và verify kết quả sau hành động; phát incident cần chẩn đoán | DB, RabbitMQ, SSH/Ceph |
| Worker | Incident + delegated-AI consumers; approved-action poller; backup scheduler; bucket logging; RGW audit; Telegram/incident outbox dispatch/reconcile; heartbeat | DB, RabbitMQ; các kết nối SSH/Ceph, AI, backup target, RGW/S3 chỉ theo loop/action cần dùng |

Dashboard còn cài các middleware dùng chung cho request ID, API observation,
rate limiting, CSRF trong production, trusted proxy/host validation, security
audit, redaction và session. Đây là dependency ngang: thay đổi middleware có
thể ảnh hưởng mọi router dù file route không đổi.

## 2. Luồng đọc và hiển thị dữ liệu

```mermaid
sequenceDiagram
  autonumber
  actor User as Operator
  participant UI as Browser UI
  participant Auth as Dashboard auth/session
  participant Route as FastAPI route
  participant Scope as Cluster/pool scope
  participant Cache as Process cache (nếu route dùng)
  participant DB as Shared database
  participant Ceph as Ceph/RGW/OpenStack

  User->>UI: Mở trang hoặc yêu cầu refresh
  UI->>Auth: HTTP request với signed session cookie
  Auth->>Route: Kiểm tra đăng nhập, product và quyền
  Route->>Scope: Resolve selected cluster + allowed resource scope
  alt API có dữ liệu cache
    Scope->>Cache: Đọc key có cluster/resource scope
    alt cache hit
      Cache-->>Route: cached data + metadata nếu endpoint cung cấp
    else cache miss / refresh
      Cache->>Ceph: Query bounded theo endpoint
      Ceph-->>Cache: result hoặc lỗi
      Cache-->>Route: fresh result hoặc stale/error tùy contract
    end
  else API đọc dữ liệu đã lưu
    Scope->>DB: Query có cluster_id/time/resource filter
    DB-->>Route: snapshot / metrics / job / audit
  else API có truy vấn live trực tiếp
    Scope->>Ceph: Query qua ceph_client hoặc integration client
    Ceph-->>Route: result hoặc timeout/error
  end
  Route-->>UI: JSON/HTML; hình dạng stale/partial tùy API
```

**Lưu ý impact:** không suy ra toàn Dashboard được bảo vệ bởi cache từ một API
có cache. Khi sửa `ceph_client`, cluster scope, cache contract, response model
hoặc polling JavaScript, lần theo tất cả route/page consumer trong graph.

## 3. Luồng Watcher, Incident và chẩn đoán AI

```mermaid
sequenceDiagram
  autonumber
  participant Ceph as Ceph MON / services
  participant Watcher as Watcher cluster loop
  participant DB as Shared database
  participant MQ as RabbitMQ incidents queue
  participant Worker as Worker incident consumer
  participant AI as Diagnosis / AI tools
  participant UI as Dashboard / Incident UI
  participant TG as Telegram

  loop Poll cadence
    Watcher->>Ceph: health + monitor/telemetry collectors
    Ceph-->>Watcher: health, checks, metrics or query error
    Watcher->>DB: heartbeat, metric/evidence, Incident dedupe/state
    opt Incident needs diagnosis
      Watcher->>MQ: persistent incident envelope + cluster context
      MQ-->>Worker: deliver incident
      Worker->>DB: mark DIAGNOSING / read Incident context
      Worker->>AI: diagnosis with bounded evidence/context
      AI-->>Worker: diagnosis/advisory or proposed action data
      Worker->>DB: persist diagnosis/action lifecycle
      opt notification enabled
        Worker->>TG: send alert/status
      end
    end
  end
  UI->>DB: load incidents / health snapshots through API
  UI->>UI: refresh on WebSocket notification or page polling
```

`watcher/remediation_main.py` là process riêng cho polling sức khỏe và
post-action verification. `watcher/main.py` có default/observed cluster loops
và một Vitastor monitoring thread; collectors phụ có thể chạy tách nền. Worker
queue dùng retry header và dead-letter topology do `shared/mq.py` khai báo.

**Realtime hiện tại:** `/ws/incidents` (`dashboard/ws.py`) kiểm tra fingerprint
của Incident trong DB theo chu kỳ 2 giây rồi gửi `incidents_changed` khi có
thay đổi. Đây không phải push trực tiếp từ RabbitMQ. Sơ đồ chưa thấy một
`action_state_changed` event dùng chung nối tới mọi UI consumer; không giả định
mọi Action update sẽ đẩy realtime tới mọi trang.

## 4. Luồng Action, approval, execution và audit

```mermaid
sequenceDiagram
  autonumber
  actor Operator
  participant UI as Dashboard / Chat / feature page
  participant API as FastAPI proposal/approval routes
  participant DB as Incident + Action + audit tables
  participant Approval as User / Telegram approval bot
  participant Worker as Worker approved-action poller
  participant Gate as Policy + preflight
  participant Exec as Allowlisted executor
  participant Target as Ceph / OpenStack / RGW / host

  Operator->>UI: Chọn thao tác
  UI->>API: Gửi target + tham số
  API->>Gate: Validate quyền, cluster scope, input và preflight
  Gate-->>API: Preview/diff + policy classification
  API->>DB: Ghi Incident/Action PENDING_APPROVAL + audit
  API-->>UI: Trả preview và trạng thái chờ duyệt
  Operator->>Approval: Approve / reject
  Approval->>DB: Ghi quyết định + actor/time
  Worker->>DB: Poll Action đã duyệt
  Worker->>Gate: Revalidate action, evidence, target và policy
  Gate-->>Worker: Allow / deny
  alt allowed
    Worker->>Exec: Dispatch action ID thuộc allowlist
    Exec->>Target: Chạy thao tác có timeout/target scope
    Target-->>Exec: Exit/result
    Exec-->>Worker: Execution result
    Worker->>Target: Post-check / reconciliation khi action hỗ trợ
    Target-->>Worker: Observed state
    Worker->>DB: Kết quả, trạng thái, evidence và audit
  else denied / execution failure / post-check failure
    Worker->>DB: FAILED/BLOCKED + lỗi đã redact + audit
  end
  UI->>API: Poll/load trạng thái Action
  API->>DB: Đọc state + audit summary
  API-->>UI: Kết quả cuối cùng
```

Một số workflow đặc biệt có orchestration riêng. Ví dụ backup schedule ở
`worker/backup/scheduler.py` tạo synthetic Incident/Action trạng thái SAFE và
dispatch trực tiếp `_execute_approved_action()` trong Worker; nó không đi qua
RabbitMQ incidents queue. Phải giữ nhánh này riêng trong graph và test.

## 5. Backup, restore và dữ liệu ngoài cụm

```mermaid
flowchart LR
  UI[Dashboard Backup / Restore]
  API[backups routes]
  DB[(Shared DB\nBackupJob / Action / Incident)]
  Worker[Worker]
  Scheduler[APScheduler coroutine\nSQLAlchemyJobStore]
  Engine[Backup / restore engine]
  Ceph[(Ceph RBD + metadata)]
  Target[(Configured SSH or S3 target)]
  Drill[Restore drill]
  Audit[Audit + digest + alerts]

  UI --> API
  API --> DB
  API -->|preview/propose| DB
  Scheduler --> DB
  Scheduler -->|scheduled SAFE action direct dispatch| Worker
  Worker --> Engine
  API -->|approved Action| DB
  Worker -->|poll approved actions| DB
  Worker --> Engine
  Engine <--> Ceph
  Engine <--> Target
  Engine --> DB
  Engine --> Drill
  Drill --> DB
  DB --> Audit
  Audit --> UI
```

Backup scheduler chạy trong cùng event loop Worker với queue consumer,
approved-action poller và bucket logging. Luồng restore từ giao diện có proposal
/approval và engine riêng; restore drill theo policy không đồng nghĩa đã có
nghiệm thu DR trên dữ liệu thật.

Incident và Telegram notification còn đi qua durable outbox để tách transaction
ghi DB khỏi việc gửi broker/bot; Worker dispatch và reconcile outbox nền.
Delegated AI dùng consumer/queue riêng, không phải incident queue.

## 6. Object Storage và S3 identity

```mermaid
flowchart LR
  User[Operator]
  UI[Bucket / Object / S3 User UI]
  API[object_storage + object_storage_users routes]
  Scope[Auth + cluster/RGW config]
  Cache[(Object storage process cache)]
  RGW[(RGW Admin / S3 API)]
  Action[(Preview / execute contract)]
  Worker[Worker executor nếu action được dispatch]
  Audit[(Object Storage audit)]

  User --> UI --> API --> Scope
  Scope --> Cache
  Cache -->|miss/expired| RGW
  Scope --> RGW
  UI --> API
  API -->|preview then execute| Action
  Action -->|approved/authorized operation as implemented| Worker
  Worker --> RGW
  API --> Audit
  Worker --> Audit
  API --> UI
```

Các endpoint inventory, detail, key/user settings và bucket governance có
contract riêng. Khi sửa, xác định trước thao tác nào được thực thi trong route
và thao tác nào được chuyển qua Worker; không coi mọi mutation của Object
Storage là cùng một executor path.

## 7. Chat AI và product boundary

```mermaid
flowchart LR
  CephUI[Ceph chat_widget.js]
  VitaUI[Vitastor chat widget]
  CephChat[dashboard/routes/chat.py]
  VitaChat[dashboard/routes/vitastor_chat.py]
  Sessions[(Chat sessions/messages/preferences)]
  Context[Context, evidence and policy]
  Provider[AI router / configured provider]
  Confirm[Confirm action endpoint]
  Action[(Incident + Action + approval)]
  Worker[Worker execution path]
  Ceph[(Ceph)]
  Vita[(Vitastor / etcd)]

  CephUI --> CephChat --> Sessions
  VitaUI --> VitaChat --> Sessions
  CephChat --> Context --> Provider
  VitaChat --> Context
  Context -->|read-only queries| Ceph
  Context -->|read-only diagnosis queries| Vita
  CephChat --> Confirm --> Action --> Worker --> Ceph
  VitaChat -->|Vitastor operation contract| Action
  Worker --> Vita
```

Ceph và Vitastor giữ route, session namespace/product authorization, cluster
models/client và action policy riêng. Shared AI libraries hoặc shell/template
không có nghĩa hai product được phép dùng lẫn cluster credentials.

## 8. Quyền, cluster scope và dữ liệu

- Browser dùng signed session cookie; Dashboard kiểm tra login và product
  namespace. Các route áp dụng dependency/role check theo endpoint.
- Ceph cluster context đến từ session/selector hoặc request parameter được
  kiểm tra với cluster đang hoạt động và resource scope. Worker cần giữ đúng
  `cluster_id`/connection context của Incident hoặc Action.
- Shared DB lưu cluster, Incident, Action, audit, metrics, backup, chat và các
  snapshot/model chuyên biệt; process caches không tự đồng bộ giữa process.
- Secret/SSH credentials phải được resolve theo cluster và không đưa vào
  browser response, event/log hay graph artifact.
- Migrations trong `alembic/` tác động model/route/worker readers và writers;
  schema change phải lan test tới tất cả consumer.

## 9. Kiến trúc lõi và biến thể theo từng nơi cài

Đúng, luồng cụ thể phụ thuộc nơi cài và cấu hình vận hành. Sơ đồ lõi mô tả
những thành phần có trong code; mỗi deployment chỉ có một phần cạnh tới hệ
thống ngoài hoặc collector hoạt động. Cần tách hai câu hỏi:

| Phần kiến trúc | Tương đối cố định theo code | Thay đổi theo deployment |
| --- | --- | --- |
| Dashboard/API, Watcher, Worker, DB và policy | Có | Có thể tách process/container hoặc dùng backing service khác |
| Ceph connection | Client/command boundary trong code | Số cluster, node/role, SSH identity, mode `docker`/`podman`/`cephadm`/`none`, version và quyền Ceph |
| Object Storage | Các route/client và worker path | RGW có cấu hình hay không, endpoint, access mode, audit/metric toggles |
| OpenStack/Cinder | Adapter và routes có trong code | Controller/credential có sẵn, version/API reachability, volume naming/mapping |
| AI | Adapter, context/policy boundary | Provider/endpoint, enable flag, model/capability, credential, network access |
| Backup/restore | Engine, scheduler, DB state | Có tracked image/policy không; target A/B dùng SSH hay S3; retention/immutability |
| Logs | Adapter interface và query pipeline | SSH collection hay Loki, node reachability, retention và source completeness |
| Telegram | Notifier/approval listener code | Bot/channel/token, quyền approver, feature toggles; một số listener chỉ chạy khi có token |
| Vitastor | Product riêng với route/client/model riêng | Có bật product và khai báo cluster active hay không; không dùng Ceph cluster scope |
| Federated IAM/Vault | Route/worker integration code | Provider, Vault address/credentials, RGW federation capability và policy |

Vì vậy graph cần có **profile triển khai**, ngoài topology code chung. Profile
được tạo từ cấu hình đã redact của từng môi trường và chỉ ghi cờ/capability,
không ghi secret. Khi profile thay đổi (ví dụ thêm RGW hoặc đổi backup target
SSH sang S3), impact selector phải bật các cạnh điều kiện tương ứng và chạy
integration contract của luồng đó. Khi chưa có profile, chạy coverage theo
toàn bộ biến thể code đã hỗ trợ hoặc fallback full non-live suite.

Manifest hiện ghi nhận các lớp biến thể và điều kiện, nhưng chưa có profile
được tạo cho từng máy cài cụ thể. Cần đối chiếu capability thực tế, Ceph
release và cấu hình đã redact ở từng nơi trước khi dùng graph để quyết định
không chạy một nhóm test.

### Federation: provider và mức hỗ trợ hiện tại

Dashboard hỗ trợ cấu hình/kiểm tra OIDC và LDAP/AD. `shared/ldap_identity.py`
thực hiện directory bind/group lookup và có thể resolve secret reference từ
environment, file cho phép hoặc Vault. Đây không đồng nghĩa mọi provider đã
được nối end-to-end vào RGW: Worker reconciliation của role mapping hiện chỉ
hỗ trợ OIDC. Vì vậy profile cài đặt phải ghi riêng `provider_type`, secret
backend và capability thực thi; không gộp chúng thành một cờ “federation bật”.

`dashboard/routes/ai_tasks.py` tồn tại/import được nhưng router hiện không được
đăng ký bằng `include_router` trong `dashboard/app.py`; nó không phải API đang
phục vụ. Graph đánh dấu riêng để tránh kiểm thử nhầm route chưa mount như tính
năng hoạt động.

## 10. Quy tắc dùng sơ đồ để khoanh vùng test

1. Ghép changed path với node `source` trong graph manifest.
2. Theo cạnh tới component consumer, API/page, event/data contract, permission
   boundary, external dependency và test.
3. Chọn test theo cả luồng nghiệp vụ và failure path; dùng permission/cluster
   scope như cạnh bắt buộc, không chỉ theo import.
4. Nếu path chưa map, graph thiếu node/edge critical, hoặc selection không chắc
   chắn, chạy full non-live suite và ghi graph gap vào impact report.
5. Thay đổi route, event, schema, permission, lifecycle hay retry/timeout phải
   cập nhật cả manifest và diagram trong cùng thay đổi code.

### Tạo bản đồ theo installation

Trang `/stream` trong nhóm Monitoring & Metrics dựng canvas theo profile của
installation đang chạy. Trang chỉ dành cho admin; node và cạnh là bản đọc cấu
hình, không phải kiểm tra kết nối hay thực thi workflow. React hiển thị sơ đồ
SVG tương tác, còn API trang tổng hợp profile trực tiếp từ settings/database.
Route, profile builder và test được map tại node
`dashboard.installation_stream`, `shared.installation_profile` và flow
`flow.installation_stream` trong manifest.

Mỗi thành phần trên Stream liên kết tới các node, flow, quan hệ và test trong
manifest `tests/architecture/graph.yaml`. Profile chỉ chuyển metadata cần để
hiển thị các liên kết này; các đường dẫn cấu hình và credential không được đưa
vào browser. Manifest cũng được đóng gói thành `docs/architecture/graph.yaml`
trong image runtime để trang có thể giải quyết liên kết khi thư mục `tests/`
không được cài đặt.

Trên từng installation, chạy từ thư mục gốc repository:

```bash
.venv/bin/python scripts/architecture_profile.py \
  --output /var/lib/ceph-ai/architecture/installation-profile.json \
  --mermaid-output /var/lib/ceph-ai/architecture/installation-flow.mmd
```

Lệnh đọc settings và các cluster/provider record trong database, không gọi Ceph,
RGW, OpenStack hay AI provider. File được ghi atomically với mode `0600`; nội
dung chỉ gồm feature status, số lượng node theo role, execution mode và loại
provider. IP, tên cluster, URL, đường dẫn key, token và secret không được xuất.
Chạy lại sau khi cấu hình thay đổi để làm mới profile. Hiện đây là thao tác CLI
tường minh, chưa tự chạy khi settings thay đổi; profile không được commit vào
repository dùng chung.

## Nguồn code chính

`dashboard/app.py`, `dashboard/routes/`, `dashboard/ws.py`,
`dashboard/cluster_scope.py`, `dashboard/static/`, `dashboard/templates/`,
`ceph-health-dashboard/src/`, `watcher/main.py`, `watcher/remediation_main.py`,
`watcher/collector.py`, `watcher/publisher.py`, `worker/main.py`,
`worker/llm/router_client.py`, `worker/executor/`, `worker/backup/`,
`shared/mq.py`, `shared/db.py`, `shared/models.py`, `shared/clusters.py`.
