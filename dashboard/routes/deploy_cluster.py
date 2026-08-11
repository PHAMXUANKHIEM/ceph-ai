import asyncio
import json
import logging
import re
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.exc import SQLAlchemyError

from config.settings import settings
from dashboard.routes import auth
from dashboard.routes.auth import require_login
from dashboard.templating import make_templates
from dashboard.vntime import format_vn_clock
from shared import audit, db
from shared.ceph_releases import codenames_oldest_first, versions_by_codename
from shared.models import Action, ActionStatus, Incident, IncidentStatus
from watcher.ceph_client import forget_host_key
from worker.executor import commands as executor_commands
from worker.executor.ssh_executor import ExecutorError
from worker.policy import gate

logger = logging.getLogger(__name__)

router = APIRouter()
templates = make_templates()

# Synthetic Incident.ceph_code for this feature — same trick
# dashboard/routes/chat.py/upgrade.py/patch.py use: AuditEntry.incident_id
# is a required FK, and building a brand-new (not-yet-monitored) cluster has
# no real detected Incident behind it, only an operator explicitly filling
# in the node table and proposing a deploy.
CLUSTER_DEPLOY_CEPH_CODE = "CLUSTER_DEPLOY"

DEPLOY_CEPHADM_ACTION_ID = "deploy_cluster_cephadm"
DEPLOY_CEPH_DEPLOY_ACTION_ID = "deploy_cluster_ceph_deploy"
DEPLOY_RPM_LOCAL_ACTION_ID = "deploy_cluster_rpm_local"
CLUSTER_DEPLOY_ACTION_IDS = frozenset(
    {DEPLOY_CEPHADM_ACTION_ID, DEPLOY_CEPH_DEPLOY_ACTION_ID, DEPLOY_RPM_LOCAL_ACTION_ID}
)

_METHOD_TO_ACTION_ID = {
    "cephadm": DEPLOY_CEPHADM_ACTION_ID,
    "ceph-deploy": DEPLOY_CEPH_DEPLOY_ACTION_ID,
    "rpm-local": DEPLOY_RPM_LOCAL_ACTION_ID,
}
# Story 8.1 wired up cephadm, Story 8.2 added ceph-deploy, Story 8.3 added
# rpm-local — all 3 methods now have a real phase sequence in
# worker/executor/cluster_deploy.py. Kept as a (now empty) gate rather than
# removed outright: the template/JS still read this list generically to
# grey out any future not-yet-supported method without a code change there.
_NOT_YET_SUPPORTED_METHODS: frozenset[str] = frozenset()

_IN_FLIGHT_ACTION_STATUSES = (ActionStatus.PENDING_APPROVAL.value, ActionStatus.APPROVED.value)

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_RPM_PATH_RE = re.compile(r"^/[A-Za-z0-9_./-]+$")
_VALID_ROLES = ("mon", "mgr", "osd", "mds", "rgw")


def _is_valid_ip(ip: str) -> bool:
    parts = ip.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not part.isdigit() or not (0 <= int(part) <= 255):
            return False
        if len(part) > 1 and part[0] == "0":
            return False  # reject ambiguous leading-zero octets (e.g. "010")
    return True


def _validate_nodes(nodes_raw) -> tuple[list[dict], str | None]:
    """Returns (normalized_nodes, error_message) — error_message is None on
    success. Normalizes each node to {"ip": str, "roles": [str, ...],
    "osd_disks": list[str]} (roles deduped, restricted to _VALID_ROLES,
    order-stable). `osd_disks` is PER NODE (not one cluster-wide value) so
    nodes with different device naming (e.g. node1 /dev/vdc, node2
    /dev/vdb) can each use their own — required and validated only for a
    node with the "osd" role; an empty list otherwise.

    2026-08-07: `osd_disk` (single string) -> `osd_disks` (list[str]) — a
    real node can carry MULTIPLE OSD disks (e.g. node1 /dev/vdc AND
    /dev/vdd, each becoming its own OSD), which a single-string field could
    never express. worker/executor/cluster_deploy.py's OSD-creation/safety
    phases now loop over this list, one `ceph-volume lvm create`/
    `ceph orch daemon add osd` call per disk."""
    if not isinstance(nodes_raw, list) or not nodes_raw:
        return [], "Cần ít nhất 1 node"

    normalized: list[dict] = []
    seen_ips: set[str] = set()
    for entry in nodes_raw:
        if not isinstance(entry, dict):
            return [], "Dữ liệu node không hợp lệ"
        ip = str(entry.get("ip", "")).strip()
        if not _is_valid_ip(ip):
            return [], f"IP không hợp lệ: {ip!r}"
        if ip in seen_ips:
            return [], f"IP bị trùng: {ip}"
        seen_ips.add(ip)

        roles_raw = entry.get("roles") or []
        if not isinstance(roles_raw, list) or any(r not in _VALID_ROLES for r in roles_raw):
            return [], f"Vai trò không hợp lệ cho node {ip}"
        roles = [r for r in _VALID_ROLES if r in roles_raw]

        osd_disks: list[str] = []
        if "osd" in roles:
            # A MISSING key defaults to [] (deferred to the "chưa điền đĩa
            # OSD" check below, run only AFTER mon/mgr/osd counts are known
            # valid — see that check's own comment for why). Only a
            # PRESENT-but-wrong-typed value (e.g. a string) is rejected
            # here, immediately.
            disks_raw = entry.get("osd_disks", [])
            if not isinstance(disks_raw, list):
                return [], f"Dữ liệu đĩa OSD không hợp lệ cho node {ip}"
            osd_disks = [str(d).strip() for d in disks_raw if str(d).strip()]
        normalized.append({"ip": ip, "roles": roles, "osd_disks": osd_disks})

    mon_count = sum(1 for n in normalized if "mon" in n["roles"])
    mgr_count = sum(1 for n in normalized if "mgr" in n["roles"])
    osd_count = sum(1 for n in normalized if "osd" in n["roles"])
    if mon_count < 1:
        return [], "Cần ít nhất 1 node MON"
    if mgr_count < 1:
        return [], "Cần ít nhất 1 node MGR"
    if osd_count < 1:
        return [], "Cần ít nhất 1 node OSD"

    # Per-node disk validation runs LAST, only once the node/role shape
    # itself is already known-valid — a structurally incomplete submission
    # (e.g. missing a MON node entirely) should surface THAT error, not an
    # unrelated disk-path complaint about a node that was never the problem.
    for n in normalized:
        if "osd" not in n["roles"]:
            continue
        if not n["osd_disks"]:
            return [], f"Chưa điền đĩa OSD cho node {n['ip']}"
        for disk in n["osd_disks"]:
            if not _RPM_PATH_RE.match(disk):
                return [], f"Đĩa OSD không hợp lệ cho node {n['ip']} (vd /dev/vdc): {disk!r}"
        if len(set(n["osd_disks"])) != len(n["osd_disks"]):
            return [], f"Đĩa OSD bị trùng lặp cho node {n['ip']}"

    return normalized, None


# 2026-07-25 (Story 8.2): unlike _PACKAGE_METHOD_SAFETY_NOTE in upgrade.py
# (which documents that the package-based UPGRADE path deliberately KEEPS
# GOING to the next host on a per-host failure), this deploy-cluster method
# does the OPPOSITE — it stops the whole deploy immediately on any single
# host's failure (AC #4) — a partially-initialized brand-new cluster is
# materially riskier than a partially-upgraded existing one, so the two
# plan-text notes must say opposite things, not share one constant.
_CEPH_DEPLOY_SAFETY_NOTE = """\
An toàn: đây là tính năng DỰNG CỤM MỚI (rủi ro cao hơn nâng cấp một cụm đang chạy) — khác với luồng
nâng cấp bằng gói hiện có (cố ý CHẠY TIẾP sang node kế tiếp nếu một node lỗi), quá trình dựng cụm
này DỪNG LẠI NGAY khi BẤT KỲ node nào lỗi ở các bước cài đặt/khởi tạo — không có node/daemon nào

Bước tạo OSD sẽ GHI/ĐỊNH DẠNG THẬT lên (một hoặc nhiều) đĩa OSD đã cấu hình cho từng node
(`ceph-volume lvm create`, một lần gọi cho MỖI đĩa) — mỗi đĩa đã được kiểm tra CHỈ ĐỌC (rỗng,
không mount) ở bước đầu tiên, nhưng thao tác tạo OSD ở bước này là không thể hoàn tác.

Lưu ý: quy trình dưới đây được viết theo tài liệu triển khai thủ công chính thức của Ceph, CHƯA
được kiểm thử trực tiếp trên một cụm ceph-deploy/gói truyền thống thật trong môi trường phát triển
này (giống lưu ý Story 7.1 đã nêu cho luồng nâng cấp bằng gói của chính nó) — lần chạy thật đầu
tiên nên được operator theo dõi sát, không nên để chạy không giám sát."""


def _deploy_plan_text(method: str, version: str, nodes: list[dict], rpm_path: str | None = None) -> str:
    mon = [n["ip"] for n in nodes if "mon" in n["roles"]]
    mgr = [n["ip"] for n in nodes if "mgr" in n["roles"]]
    # Per-node disk LIST (vd node1 /dev/vdc + /dev/vdd, node2 /dev/vdb) shown
    # explicitly here so the operator can double check EACH node's disk
    # assignment before Duyệt, not just that osd_disks is set at all.
    osd = [f"{n['ip']} ({', '.join(n.get('osd_disks') or [])})" for n in nodes if "osd" in n["roles"]]
    # RGW is OPTIONAL, unlike mon/mgr/osd above (see worker/executor/
    # cluster_deploy.py::_phase_ceph_deploy_rgw_create's own docstring) — an
    # empty rgw list is a normal, valid cluster, so this line always shows
    # (possibly blank) rather than being hidden entirely.
    rgw = [n["ip"] for n in nodes if "rgw" in n["roles"]]
    node_summary = (
        f"MON: {', '.join(mon)}\nMGR: {', '.join(mgr)}\nOSD: {', '.join(osd)}\n"
        f"RGW: {', '.join(rgw) if rgw else '(không có)'}"
    )
    rgw_note = (
        f" (kèm {len(rgw)} node RGW)" if rgw else " (không có node RGW — bỏ qua bước tạo RGW)"
    )

    if method == "cephadm":
        return (
            f"Dựng cụm Ceph {version} MỚI qua cephadm trên {len(nodes)} node.\n{node_summary}\n\n"
            f"Các bước sẽ thực hiện, LẦN LƯỢT, sau khi Duyệt:\n"
            f"1. Kiểm tra kết nối SSH, hệ điều hành, và đĩa OSD trên từng node — CHỈ ĐỌC, không "
            f"ghi gì. Nếu đĩa OSD đã có dữ liệu hoặc hệ điều hành không được hỗ trợ, dừng lại "
            f"ngay, chưa cài đặt gì.\n"
            f"2. `cephadm bootstrap --mon-ip {mon[0] if mon else '<mon>'}` trên node MON đầu tiên.\n"
            f"3. `ceph orch host add` cho các node còn lại.\n"
            f"4. `ceph orch apply mgr` cho các node MGR.\n"
            f"5. `ceph orch daemon add osd` cho từng node OSD, dùng đúng đĩa đã cấu hình (không "
            f"bao giờ dùng --all-available-devices).\n"
            f"6. `ceph orch apply rgw` cho các node RGW{rgw_note}.\n"
            f"7. Kiểm tra `ceph -s` — dừng lại nếu HEALTH_ERR.\n\n"
            f"các bước sau không chạy."
        )
    if method == "ceph-deploy":
        return (
            f"Dựng cụm Ceph {version} MỚI qua ceph-deploy (cài gói truyền thống) trên {len(nodes)} "
            f"node.\n{node_summary}\n\n"
            f"Các bước sẽ thực hiện, LẦN LƯỢT, sau khi Duyệt:\n"
            f"1. Kiểm tra kết nối SSH, hệ điều hành, và đĩa OSD trên từng node — CHỈ ĐỌC, không "
            f"ghi gì. Nếu đĩa OSD đã có dữ liệu hoặc hệ điều hành không được hỗ trợ, dừng lại "
            f"ngay, chưa cài đặt gì.\n"
            f"2. Cài phụ thuộc (chrony, tắt firewalld/SELinux) trên từng node.\n"
            f"3. Cấu hình repo gói Ceph chính thức từ download.ceph.com cho bản {version}.\n"
            f"4. Cài gói Ceph theo vai trò từng node (ceph-mon/ceph-mgr/ceph-osd/ceph-radosgw — "
            f"node nhiều vai trò được cài nhiều gói).\n"
            f"5. Khởi tạo MON thủ công (fsid, monmap, keyring, mkfs) và khởi động ceph-mon.\n"
            f"6. Chờ MON đạt quorum (tối đa vài phút).\n"
            f"7. Tạo và khởi động MGR.\n"
            f"8. Tạo OSD bằng `ceph-volume lvm create`, dùng đúng đĩa đã cấu hình.\n"
            f"9. Tạo và khởi động RGW (radosgw, cổng 7480) cho từng node RGW{rgw_note}.\n"
            f"10. Kiểm tra `ceph -s` — dừng lại nếu HEALTH_ERR.\n\n"
            f"{_CEPH_DEPLOY_SAFETY_NOTE}"
        )
    if method == "rpm-local":
        path = rpm_path or "?"
        return (
            f"Dựng cụm Ceph {version} MỚI qua RPM local (gói cục bộ, không cần Internet) trên "
            f"{len(nodes)} node.\n{node_summary}\n\n"
            f"Các bước sẽ thực hiện, LẦN LƯỢT, sau khi Duyệt:\n"
            f"1. Kiểm tra kết nối SSH, hệ điều hành, và đĩa OSD trên từng node — CHỈ ĐỌC, không "
            f"ghi gì. Nếu đĩa OSD đã có dữ liệu hoặc hệ điều hành không được hỗ trợ, dừng lại "
            f"ngay, chưa cài đặt gì.\n"
            f"2. Cài phụ thuộc (chrony, tắt firewalld/SELinux) trên từng node.\n"
            f"3. Kiểm tra thư mục `{path}` tồn tại và có gói trên từng node, rồi dựng repo cục bộ "
            f"ngay tại đó (`createrepo`/`dpkg-scanpackages`) — KHÔNG thêm repo download.ceph.com.\n"
            f"4. Cài gói Ceph theo vai trò từng node (ceph-mon/ceph-mgr/ceph-osd/ceph-radosgw — "
            f"node nhiều vai trò được cài nhiều gói) từ repo cục bộ này.\n"
            f"5. Khởi tạo MON thủ công (fsid, monmap, keyring, mkfs) và khởi động ceph-mon.\n"
            f"6. Chờ MON đạt quorum (tối đa vài phút).\n"
            f"7. Tạo và khởi động MGR.\n"
            f"8. Tạo OSD bằng `ceph-volume lvm create`, dùng đúng đĩa đã cấu hình.\n"
            f"9. Tạo và khởi động RGW (radosgw, cổng 7480) cho từng node RGW{rgw_note}.\n"
            f"10. Kiểm tra `ceph -s` — dừng lại nếu HEALTH_ERR.\n\n"
            f"{_CEPH_DEPLOY_SAFETY_NOTE}\n\n"
            f"KHÔNG có gì được tải từ Internet — toàn bộ gói Ceph phải đã được đặt sẵn tại CÙNG "
            f"đường dẫn `{path}` trên MỌI node đã cấu hình, từ TRƯỚC khi đề xuất."
        )
    return (
        f"Dựng cụm Ceph {version} mới qua {method} trên {len(nodes)} node.\n{node_summary}\n\n"
        f"Phương thức này chưa được hỗ trợ tự động trong phiên bản hiện tại."
    )


def _step_clock(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return format_vn_clock(datetime.fromisoformat(value))
    except ValueError:
        return None


def _with_step_display_times(progress: list) -> list:
    """Adds started_at_display/finished_at_display (Vietnam-local HH:MM:SS,
    or None if that step hasn't reached that point yet) to each step dict
    from worker/executor/cluster_deploy.py's progress list — called from
    BOTH the initial page load below and the polling endpoint further down,
    so the live log's per-line time prefix is identical whichever one fed
    it. Fixes a real bug: deploy_cluster.js used to stamp EVERY line with
    the browser's current clock on every single poll tick, so an
    already-`done`/`failed` step's displayed time kept drifting to "now"
    forever instead of freezing at when that step actually finished."""
    for step in progress:
        if isinstance(step, dict):
            step["started_at_display"] = _step_clock(step.get("started_at"))
            step["finished_at_display"] = _step_clock(step.get("finished_at"))
    return progress


@router.get("/deploy-cluster", response_class=HTMLResponse)
async def deploy_cluster_page(request: Request, user: str = Depends(require_login)):
    try:
        with db.SessionLocal() as session:
            last_action = (
                session.query(Action)
                .filter(Action.action_id.in_(CLUSTER_DEPLOY_ACTION_IDS))
                .order_by(Action.created_at.desc())
                .first()
            )
    except SQLAlchemyError:
        logger.exception("deploy_cluster_page: failed to query DB")
        raise HTTPException(
            status_code=503,
            detail="Không kết nối được database — đã chạy `alembic upgrade head` chưa?",
        )

    pending_action = (
        last_action if last_action is not None and last_action.status in _IN_FLIGHT_ACTION_STATUSES else None
    )

    last_action_params: dict = {}
    if last_action is not None and last_action.action_params:
        try:
            last_action_params = json.loads(last_action.action_params) or {}
        except (TypeError, ValueError):
            last_action_params = {}

    progress: list = []
    if last_action is not None and last_action.execution_progress:
        try:
            progress = json.loads(last_action.execution_progress) or []
        except (TypeError, ValueError):
            progress = []
    progress = _with_step_display_times(progress)

    return templates.TemplateResponse(
        request,
        "deploy_cluster.html",
        {
            "user": user,
            "is_admin": auth.is_admin_user(user),
            "codenames": codenames_oldest_first(),
            "versions_by_codename": versions_by_codename(),
            "default_ssh_user": settings.ssh_user,
            "default_ssh_key_path": settings.ssh_key_path,
            "not_yet_supported_methods": sorted(_NOT_YET_SUPPORTED_METHODS),
            "pending_action": pending_action,
            "last_action": last_action,
            "last_action_params": last_action_params,
            "progress": progress,
        },
    )


@router.post("/deploy-cluster/forget-host-key")
async def deploy_cluster_forget_host_key(request: Request, user: str = Depends(require_login)):
    """Clears one node's pinned SSH host key (trust-on-first-use,
    watcher/ceph_client.py::forget_host_key(), same KNOWN_HOSTS_PATH file
    worker/executor/ssh_executor.py also reads/writes) so a re-provisioned
    node (OS reinstalled -> new host key) can be connected to again without
    SSHing into this server to hand-edit the known_hosts file.

    Deliberately NOT a always-visible Settings-page form anymore (that's
    where this started — see the "Add Dashboard control to clear a stale
    SSH host key" commit) — it's a niche recovery action an operator only
    ever needs at the exact moment `_phase_ssh_check` in
    worker/executor/cluster_deploy.py fails with paramiko's
    BadHostKeyException ("Host key for server '<ip>' does not match: got
    '...', expected '...'"). deploy_cluster.js now detects that exact
    failure inline in the deploy log and renders a button that POSTs here
    for just that node, right where the operator is already looking,
    instead of a control they'd have to know to go find on a different
    page days apart from when it's ever relevant. Admin-only: this removes
    the guard against a swapped/MITM'd node, same posture as every other
    admin-gated action in this app."""
    if not auth.is_admin_user(user):
        raise HTTPException(
            status_code=403,
            detail="Chỉ tài khoản admin mới được phép thực hiện thao tác này",
        )
    body = await request.json()
    host = str(body.get("host", "")).strip()
    if not host:
        raise HTTPException(status_code=400, detail="Thiếu địa chỉ IP/hostname của node")
    removed = await asyncio.to_thread(forget_host_key, host)
    if removed:
        return JSONResponse(
            {
                "success": True,
                "message": f"Đã xoá SSH host key cũ của {host} — điền lại form bên trái và bấm "
                f"\"Bắt đầu cài đặt\" để chạy lại (lần kết nối tiếp theo sẽ tự lưu key mới).",
            }
        )
    return JSONResponse(
        {
            "success": False,
            "message": f"Không tìm thấy host key đã lưu cho {host} (có thể đã được xoá trước đó, hoặc "
            f"chưa từng kết nối SSH thành công tới host này).",
        }
    )


@router.post("/deploy-cluster/propose")
async def propose_deploy(request: Request, user: str = Depends(require_login)):
    body = await request.json()

    version = str(body.get("version", "")).strip()
    if not _VERSION_RE.match(version):
        raise HTTPException(
            status_code=400, detail="Phiên bản không hợp lệ (định dạng x.y.z, vd 18.2.8)"
        )

    method = str(body.get("method", "")).strip()
    action_id = _METHOD_TO_ACTION_ID.get(method)
    if action_id is None:
        raise HTTPException(status_code=400, detail=f"Phương thức không hợp lệ: {method!r}")
    if method in _NOT_YET_SUPPORTED_METHODS:
        raise HTTPException(
            status_code=400,
            detail="Phương thức này chưa được hỗ trợ tự động — chọn cephadm, hoặc chờ story tiếp theo",
        )

    nodes, nodes_error = _validate_nodes(body.get("nodes"))
    if nodes_error:
        raise HTTPException(status_code=400, detail=nodes_error)

    rpm_path = str(body.get("rpm_path", "")).strip()
    if method == "rpm-local" and not _RPM_PATH_RE.match(rpm_path):
        raise HTTPException(
            status_code=400, detail="Đường dẫn thư mục RPM không hợp lệ (vd /opt/ceph-rpms)"
        )

    public_network = str(body.get("public_network", "")).strip()
    cluster_network = str(body.get("cluster_network", "")).strip() or public_network

    try:
        osd_pool_default_size = int(body.get("osd_pool_default_size", 3))
        osd_pool_default_min_size = int(body.get("osd_pool_default_min_size", 2))
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=400, detail="osd_pool_default_size/osd_pool_default_min_size phải là số nguyên"
        )

    action_params = {
        "version": version,
        "method": method,
        "nodes": nodes,
        "public_network": public_network,
        "cluster_network": cluster_network,
        "osd_pool_default_size": osd_pool_default_size,
        "osd_pool_default_min_size": osd_pool_default_min_size,
    }
    if method == "rpm-local":
        action_params["rpm_path"] = rpm_path

    target_nodes = [n["ip"] for n in nodes]
    try:
        preview_command = executor_commands.get_command(action_id, target_nodes[0], action_params)
    except ExecutorError as exc:
        raise HTTPException(status_code=400, detail=f"Không tạo được lệnh xem trước: {exc}")

    with db.SessionLocal() as session:
        existing = (
            session.query(Action)
            .filter(Action.action_id.in_(CLUSTER_DEPLOY_ACTION_IDS))
            .filter(Action.status.in_(_IN_FLIGHT_ACTION_STATUSES))
            .first()
        )
        if existing is not None:
            raise HTTPException(
                status_code=409,
                detail="Đã có một đề xuất dựng cụm đang chờ duyệt hoặc đã duyệt — không thể tạo thêm.",
            )

        incident = Incident(
            ceph_code=CLUSTER_DEPLOY_CEPH_CODE,
            status=IncidentStatus.PENDING_APPROVAL.value,
            log_excerpt=(
                f"Đề xuất dựng cụm Ceph mới ({method}, {version}) bởi {user} — {len(nodes)} node"
            ),
            detected_at=datetime.utcnow(),
        )
        session.add(incident)
        session.flush()  # assigns incident.id, needed by the Action FK below

        action = Action(
            incident_id=incident.id,
            action_id=action_id,
            classification=gate.classify_action(action_id).value,  # always RISKY (AD-5)
            status=ActionStatus.PENDING_APPROVAL.value,
            rationale=_deploy_plan_text(method, version, nodes, rpm_path),
            target_nodes=json.dumps(target_nodes),
            action_params=json.dumps(action_params),
            proposed_command=preview_command,
        )
        session.add(action)
        session.flush()

        audit.record(
            session,
            incident_id=incident.id,
            action_id=action.id,
            event_type=audit.EVENT_RISKY_ACTION_PENDING_APPROVAL,
            actor=user,
        )
        session.commit()
        action_pk = action.id

    return JSONResponse({"action_id": action_pk}, status_code=201)


@router.get("/deploy-cluster/progress")
async def deploy_cluster_progress(user: str = Depends(require_login)):
    with db.SessionLocal() as session:
        action = (
            session.query(Action)
            .filter(Action.action_id.in_(CLUSTER_DEPLOY_ACTION_IDS))
            .order_by(Action.created_at.desc())
            .first()
        )
        if action is None:
            return JSONResponse({"status": None, "progress": []})
        try:
            progress = json.loads(action.execution_progress) if action.execution_progress else []
        except (TypeError, ValueError):
            progress = []
        progress = _with_step_display_times(progress)
        return JSONResponse({"status": action.status, "progress": progress})
