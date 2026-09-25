# RBAC và cross-cluster action/route matrix

_­p n~­t: 2026-09-25  
Ph¡m vi: Ceph dashboard, WebSocket, Worker và typed remediation gateway.  
Måc tiêu: mô ~£ boundary hÇn có ts¾Ûc khi ~Õ sung capability theo action/cluster.

## Quy!°Ûc

- read:!Íc snapshot/evidence h·c trang dashboard.
- preview: ~ñng command/plan, ci°a ch¡y xÑng Ceph.
- execute: ~¡o action h·c thñc thi mutation sau approval/preflight.
- admin: q£n trË tài kho£n, cluster, policy và runtime controls.
- destructive: xóa, purge, trash force remove, lifecycle thay!Õi åm.
- cluster scope: request ph£i resolve mÙt cluster cå t~Ã; không!°ãc dùng target/evidence ça cluster khác.

## Matrix hiÇn ~¡i

| Nhóm route/consumer | Read | Preview | Execute | Admin/destructive guard | Cluster scope hÇn có | KÃm t~í hÇn có | Kh£ng t~Ñng |
|---|---:|---:|---:|---|---|---|---|
| Dashboard pages và read-only API | require_login |  |  | Không c§n admin cho read | Session/query chÍn cluster; nhiÁu API!ã ~Íc cluster_id | Dashboard auth, cluster scope | Ci°a có user-to-cluster grant riêng; user authenticated có thÃ!Íc Íi active cluster |
| /api/volumes/* mutation proposals |  | Admin | Qua Action/approval | _require_admin_privilege; destructive còn qua policy | Resolve cluster và ¯n cluster_id vào Incident/Action; có idempotency key | Volume policy/action tests | Ci°a có matrix capability theo ~ëng user/action; §n test stale evidence và Õi cluster |
| Action approval/rejection |  |  | Approval/execute | Destructive approval admin-only; policy gate | Action liên k¿t Incident; execution!Íc ~¡i cluster | Policy/preflight/action tests | _§n route/API/WebSocket matrix và test target cluster mismatch |
| Cluster lifecycle, upgrade, patch, restore, backup | Read tùy route | Admin | Admin/approval | C~ç ¿u is_admin_user h·c require_default_cluster | _Ùt sÑ route c~É cho default cluster; Ùt Ñ route nh­n cluster | Route-specific tests | _§n Ùt contract chung thay cho guard r£i rác; c§n c~éng minh cross-cluster rejection |
| Object/RGW management | Read | Admin | Admin + capability/preflight tùy action | NhÁu route is_admin_user | Cluster!°ãc resolve të request | Object-storage tests | Ci°a thÑng nh¥t capability names và user-to-cluster authorization |
| WebSocket /ws/incidents, /ws/cluster-state | Có |  | Không | _session_is_valid; ch·n Vitastor | C~Ín cluster ~ë session; cluster_id query khác session ~Ë óng 1008>ß cluster-state | WebSocket/auth tests Ùt p~§n | Ci°a có capability check read theo cluster; incidents socket ch°a reject query cluster mismatch |
| Worker Incident consumer | Evidence |  | Diagnosis/remediation | Typed gateway, policy, preflight, scope/evidence checks | Envelope m°u cluster_id và credential context; execution resolve cluster ~ë Incident | Worker/outbox/policy tests | C§n test replay envelope ~Ûi target/evidence cça cluster khác |
| Telegram approval path |  |  | Approval | Chat authorization + core action checks | Core action dùng Incident cluster | Approval tests mÙt p~§n | _§n audit matrix và cross-cluster negative tests |
| Capability Matrix admin | Read | AI proposal | Approve/create/deprecate | Admin-only | Capability inventory theo cluster/version | Capability matrix tests |!ây là Ceph-version capability, ch°a thay t~¿ RBAC user/action/cluster |

## Boundary ~¯t buÙc cho 5.3

1. cluster_id ça request, evidence fingerprint, Incident và Action ph£i cùng mÙt cluster.
2.!Õi cluster sau khi ~¡o preview p~£i làm preview ci h¿t hÇu lñc và yêu §u t¡o ~¡i.
3. Target node/pool/image p~£i!°ãc kÃm tra ~¡i ~¡i execution-time trên cluster ã m°u trong Action.
4. WebSocket p~£i xác t~ñc session và ~ë c~Ñi cluster query khác session; Íi endpoint mutation ph£i ~ë c~Ñi th¿u capability>ß server, không dña vào>©n nút UI.
5. Audit event ph£i có: actor, user role/capability, cluster, action, target, evidence fingerprint, decision và lý do të c~Ñi.
6. Worker không!°ãc dùng cluster ·c!Ënh khi envelope/Incident!ã c~É rõ cluster khác.
7. Full-access/Single Full ~«n giï toàn quÁn theo c~ç tr±¡ng v­n hành, ni°ng Íi request thuÙc ¾Ýng typed remediation p~£i!i qua boundary server-side nêu trên.

## Tr¡ng thái nghÇm thu

- Matrix route/action:!ã ~­p, §n operator review.
- Direct API th¿u quÁn: ci°a c~¡y!§y!ç.
- WebSocket th¿u quÁn/cross-cluster: Ûi có guard mÙt p~§n.
- Worker cross-cluster replay: ci°a có test riêng.
- Audit completeness: ci°a có test contract thÑng nh¥t.
- Ch°a ¾ãc coi là ¡t 5.3 cho ¿n khi có negative tests và evidence trên ~¥t c£ boundary.
