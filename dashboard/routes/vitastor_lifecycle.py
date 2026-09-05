"""Deploy/delete workflows for Vitastor, independent from Ceph actions."""

import json
import re
from datetime import datetime

from sqlalchemy import exists, or_
from sqlalchemy.exc import IntegrityError
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse

from dashboard.routes import auth
from dashboard.routes.vitastor import require_vitastor_login
from dashboard.templating import make_templates
from shared import db
from shared.models import VitastorCluster, VitastorOperation
from vitastor.operations import backup, delete, deploy, upgrade

router = APIRouter(prefix="/vitastor", tags=["vitastor-lifecycle"])
templates = make_templates()
HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.-]{0,253}$")
DISK_RE = re.compile(r"^/dev/[A-Za-z0-9._/+:-]+$")
VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+:~_-]{0,63}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,254}$")
SNAPSHOT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
BACKUP_METHODS = {"snapshot", "full_qcow2", "incremental_qcow2", "raw", "metadata_etcd", "metadata_antietcd"}
IN_FLIGHT = ("PENDING_APPROVAL", "RUNNING")


def _active_cluster(session, cluster_id: str):
    return session.query(VitastorCluster).filter(
        VitastorCluster.id == cluster_id,
        VitastorCluster.is_active.is_(True),
    ).first()


def _require_admin(user: str) -> None:
    if not auth.is_vitastor_admin_user(user):
        raise HTTPException(403, "Chỉ Vitastor Admin được deploy hoặc delete cụm")


def _latest(operation: str):
    with db.SessionLocal() as session:
        row = session.query(VitastorOperation).filter_by(operation=operation).order_by(VitastorOperation.created_at.desc()).first()
        if row:
            session.expunge(row)
        return row


def _context(user: str, operation: str) -> dict:
    row = _latest(operation)
    with db.SessionLocal() as session:
        clusters = session.query(VitastorCluster).order_by(VitastorCluster.name).all()
        session.expunge_all()
    return {"user": user, "is_admin": True, "operation": row, "progress": json.loads(row.progress_json) if row else [], "clusters": clusters}


def _validate_nodes(raw) -> list[dict]:
    if not isinstance(raw, list) or not raw:
        raise HTTPException(400, "Cần ít nhất một node")
    nodes, seen = [], set()
    for item in raw:
        host = str(item.get("host") or "").strip() if isinstance(item, dict) else ""
        roles = item.get("roles") or [] if isinstance(item, dict) else []
        disks = [str(v).strip() for v in (item.get("disks") or [])] if isinstance(item, dict) else []
        if not HOST_RE.fullmatch(host) or host in seen:
            raise HTTPException(400, f"Host không hợp lệ hoặc bị trùng: {host!r}")
        if not isinstance(roles, list) or any(role not in {"mon", "osd"} for role in roles):
            raise HTTPException(400, f"Role không hợp lệ trên {host}")
        if "osd" in roles and not disks:
            raise HTTPException(400, f"Node OSD {host} chưa có thiết bị")
        if any(not DISK_RE.fullmatch(disk) for disk in disks) or len(disks) != len(set(disks)):
            raise HTTPException(400, f"Danh sách thiết bị không hợp lệ trên {host}")
        seen.add(host); nodes.append({"host": host, "roles": sorted(set(roles)), "disks": disks})
    if not any("mon" in node["roles"] for node in nodes):
        raise HTTPException(400, "Cần ít nhất một monitor")
    if not any("osd" in node["roles"] for node in nodes):
        raise HTTPException(400, "Cần ít nhất một OSD")
    all_disks = [(node["host"], disk) for node in nodes for disk in node["disks"]]
    if len(all_disks) != len(set(all_disks)):
        raise HTTPException(400, "Thiết bị OSD bị trùng")
    return nodes


def _known_upgrade_hosts(cluster) -> set[str]:
    """Return hosts discovered from the cluster connection and cached topology."""
    hosts = {str(cluster.management_host or "").strip()}
    try:
        cached = json.loads(cluster.last_status_json or "{}")
    except (TypeError, ValueError):
        cached = {}
    if not isinstance(cached, dict):
        cached = {}

    deployment = cached.get("deployment")
    if isinstance(deployment, dict):
        for node in deployment.get("nodes") or []:
            if isinstance(node, dict) and str(node.get("host") or "").strip():
                hosts.add(str(node["host"]).strip())

    for item in cached.get("osds") or []:
        if not isinstance(item, dict) or item.get("type") != "osd":
            continue
        parent = str(item.get("parent") or "").strip()
        if parent:
            hosts.add(parent.split("/", 1)[0])

    # A manually-connected cluster has no deployment object; its Etcd
    # endpoints still identify monitor nodes that may need upgrading.
    for endpoint in str(cluster.etcd_address or "").split(","):
        endpoint = endpoint.strip()
        if not endpoint:
            continue
        hosts.add(endpoint.rsplit(":", 1)[0] if ":" in endpoint else endpoint)
    return {host for host in hosts if host}


def _create_operation(operation: str, cluster_name: str, params: dict, plan: str, user: str, cluster_id=None):
    with db.SessionLocal() as session:
        if session.query(VitastorOperation).filter(VitastorOperation.status.in_(IN_FLIGHT)).first():
            raise HTTPException(409, "Đang có một thao tác Vitastor chờ duyệt hoặc đang chạy")
        row = VitastorOperation(operation=operation, cluster_id=cluster_id, cluster_name=cluster_name, params_json=json.dumps(params), plan_text=plan, progress_json="[]", requested_by=user)
        session.add(row)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            raise HTTPException(409, "Đang có một thao tác Vitastor chờ duyệt hoặc đang chạy") from exc
        session.refresh(row)
        return {"operation_id": row.id, "status": row.status}


def _execute(operation_id: str) -> None:
    with db.SessionLocal() as session:
        row = session.get(VitastorOperation, operation_id)
        if not row or row.status != "RUNNING": return
        if row.cluster_id and not _active_cluster(session, row.cluster_id):
            row.status = "FAILED"
            row.error_message = "Cụm Vitastor đã bị vô hiệu hoá hoặc xoá trước khi chạy thao tác"
            row.finished_at = datetime.utcnow()
            session.commit()
            return
        operation, params = row.operation, json.loads(row.params_json)
    steps: dict[str, dict] = {}
    def progress(step: str, status: str, message: str):
        now = datetime.utcnow().isoformat()
        entry = steps.setdefault(step, {"id": step, "started_at": now})
        entry.update({"status": status, "message": message})
        if status in {"done", "failed"}: entry["finished_at"] = now
        with db.SessionLocal() as session:
            current = session.get(VitastorOperation, operation_id)
            current.progress_json = json.dumps(list(steps.values())); session.commit()
    try:
        handlers = {"deploy": deploy, "delete": delete, "upgrade": upgrade, "backup": backup}
        if operation not in handlers:
            raise RuntimeError(f"Thao tác Vitastor không được hỗ trợ: {operation}")
        handlers[operation](params, progress)
        with db.SessionLocal() as session:
            row = session.get(VitastorOperation, operation_id)
            if operation == "deploy":
                monitors = [n["host"] for n in params["nodes"] if "mon" in n["roles"]]
                cluster = VitastorCluster(name=row.cluster_name, management_host=monitors[0], etcd_address=",".join(f"{h}:2379" for h in monitors), etcd_prefix=params["etcd_prefix"], config_path="/etc/vitastor/vitastor.conf", ssh_user=params["ssh_user"], ssh_key_path=params["ssh_key_path"], exec_mode="none", container_name="", is_active=True, last_status_json=json.dumps({"deployment": params}), last_checked_at=datetime.utcnow(), created_by=row.requested_by)
                session.add(cluster); session.flush(); row.cluster_id = cluster.id
            elif operation == "delete":
                cluster = session.get(VitastorCluster, row.cluster_id)
                if cluster: session.delete(cluster)
            row.status, row.finished_at = "SUCCESS", datetime.utcnow(); session.commit()
    except Exception as exc:
        error_message = str(exc)
        if operation == "deploy":
            error_message += " | Deploy có thể đã thay đổi một phần; giữ nguyên package/dữ liệu để kiểm tra thủ công, không tự rollback."
        elif operation == "upgrade":
            error_message += " | Upgrade đã dừng tại node lỗi; package các node trước đó không được tự rollback."
        progress("error", "failed", error_message)
        with db.SessionLocal() as session:
            row = session.get(VitastorOperation, operation_id)
            row.status, row.error_message, row.finished_at = "FAILED", error_message, datetime.utcnow(); session.commit()


@router.get("/deploy-cluster", response_class=HTMLResponse)
async def deploy_page(request: Request, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    return templates.TemplateResponse(request, "vitastor/deploy_cluster.html", _context(user, "deploy"))


@router.post("/deploy-cluster/propose")
async def propose_deploy(request: Request, user: str = Depends(require_vitastor_login)):
    _require_admin(user); body = await request.json()
    name = str(body.get("cluster_name") or "").strip()
    if not name: raise HTTPException(400, "Tên cụm không được để trống")
    with db.SessionLocal() as session:
        if session.query(VitastorCluster).filter_by(name=name).first(): raise HTTPException(409, "Tên cụm đã tồn tại")
    nodes = _validate_nodes(body.get("nodes"))
    params = {"nodes": nodes, "ssh_user": str(body.get("ssh_user") or "").strip(), "ssh_key_path": str(body.get("ssh_key_path") or "").strip(), "etcd_prefix": str(body.get("etcd_prefix") or "/vitastor").strip(), "osd_network": str(body.get("osd_network") or "").strip(), "install_packages": bool(body.get("install_packages"))}
    if not params["ssh_user"] or not params["ssh_key_path"] or not params["osd_network"]: raise HTTPException(400, "SSH user, SSH key và OSD network là bắt buộc")
    disks = ", ".join(f"{n['host']}: {', '.join(n['disks'])}" for n in nodes if n["disks"])
    package_step = "Cài gói vitastor + etcd bằng package manager" if params["install_packages"] else "Kiểm tra gói vitastor + etcd đã được cài sẵn"
    plan = f"DEPLOY CỤM VITASTOR {name}\nMonitor: {', '.join(n['host'] for n in nodes if 'mon' in n['roles'])}\nOSD: {disks}\n\n1. Preflight SSH/thiết bị (chỉ đọc)\n2. {package_step}\n3. Ghi vitastor.conf\n4. Khởi tạo Etcd và monitor\n5. vitastor-disk prepare (GHI VĨNH VIỄN lên thiết bị)\n6. Kiểm tra vitastor-cli status"
    return _create_operation("deploy", name, params, plan, user)


@router.get("/delete-cluster", response_class=HTMLResponse)
async def delete_page(request: Request, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    return templates.TemplateResponse(request, "vitastor/delete_cluster.html", _context(user, "delete"))


@router.post("/delete-cluster/propose")
async def propose_delete(request: Request, user: str = Depends(require_vitastor_login)):
    _require_admin(user); body = await request.json(); cluster_id = str(body.get("cluster_id") or "")
    with db.SessionLocal() as session:
        cluster = _active_cluster(session, cluster_id)
        if not cluster: raise HTTPException(404, "Không tìm thấy cụm đang hoạt động")
        try: deployment = json.loads(cluster.last_status_json or "{}").get("deployment")
        except ValueError: deployment = None
        if not deployment: raise HTTPException(400, "Cụm được kết nối thủ công không có topology deploy để xoá tự động")
        name = cluster.name
    if body.get("confirmation") != name: raise HTTPException(400, f"Nhập chính xác tên cụm {name!r} để xác nhận")
    params = dict(deployment); params["wipe_disks"] = bool(body.get("wipe_disks"))
    plan = f"DELETE CỤM VITASTOR {name}\n1. Kiểm tra SSH\n2. {'PURGE VĨNH VIỄN thiết bị OSD khi Etcd còn hoạt động' if params['wipe_disks'] else 'Giữ nguyên dữ liệu thiết bị OSD'}\n3. Dừng Vitastor/monitor/Etcd\n4. Vô hiệu hoá service và xoá cấu hình"
    return _create_operation("delete", name, params, plan, user, cluster_id)


@router.get("/upgrade", response_class=HTMLResponse)
async def upgrade_page(request: Request, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    return templates.TemplateResponse(request, "vitastor/upgrade.html", _context(user, "upgrade"))


@router.post("/upgrade/propose")
async def propose_upgrade(request: Request, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    body = await request.json()
    cluster_id = str(body.get("cluster_id") or "")
    target = str(body.get("target_version") or "").strip()
    if not VERSION_RE.fullmatch(target):
        raise HTTPException(400, "Phiên bản đích không hợp lệ")
    raw_nodes = body.get("nodes")
    if not isinstance(raw_nodes, list):
        raise HTTPException(400, "Danh sách node không hợp lệ")
    nodes = []
    for value in raw_nodes:
        host = str(value or "").strip()
        if not HOST_RE.fullmatch(host) or host in nodes:
            raise HTTPException(400, f"Host không hợp lệ hoặc bị trùng: {host!r}")
        nodes.append(host)
    if not nodes:
        raise HTTPException(400, "Cần ít nhất một node để upgrade")
    with db.SessionLocal() as session:
        cluster = _active_cluster(session, cluster_id)
        if not cluster:
            raise HTTPException(404, "Không tìm thấy cụm đang hoạt động")
        unknown_hosts = set(nodes) - _known_upgrade_hosts(cluster)
        if unknown_hosts:
            raise HTTPException(
                400,
                "Node không thuộc topology Vitastor đã biết: " + ", ".join(sorted(unknown_hosts)),
            )
        params = {
            "nodes": nodes, "target_version": target,
            "management_host": cluster.management_host, "ssh_user": cluster.ssh_user,
            "ssh_key_path": cluster.ssh_key_path, "etcd_address": cluster.etcd_address,
            "etcd_prefix": cluster.etcd_prefix, "config_path": cluster.config_path,
            "exec_mode": cluster.exec_mode, "container_name": cluster.container_name,
        }
        name = cluster.name
    plan = (
        f"UPGRADE CỤM VITASTOR {name} → {target}\n"
        f"Thứ tự node: {' → '.join(nodes)}\n\n"
        "1. Chỉ bắt đầu khi cụm HEALTHY và SSH hoạt động\n"
        "2. Upgrade package + restart vitastor.target từng node\n"
        "3. Kiểm tra cụm HEALTHY sau MỖI node; lỗi sẽ dừng ngay\n"
        "4. Xác minh phiên bản và sức khoẻ cuối cùng\n\n"
        "Lưu ý: không tự động rollback package; VM/client nên được nâng cấp sau server."
    )
    return _create_operation("upgrade", name, params, plan, user, cluster_id)


@router.get("/backup", response_class=HTMLResponse)
async def backup_page(request: Request, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    return templates.TemplateResponse(request, "vitastor/backup.html", _context(user, "backup"))


@router.post("/backup/propose")
async def propose_backup(request: Request, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    body = await request.json()
    cluster_id = str(body.get("cluster_id") or "")
    method = str(body.get("method") or "")
    if method not in BACKUP_METHODS:
        raise HTTPException(400, "Phương thức backup không hợp lệ")
    image = str(body.get("image") or "").strip()
    snapshot = str(body.get("snapshot") or "").strip()
    destination = str(body.get("destination") or "").strip()
    backing_file = str(body.get("backing_file") or "").strip()
    image_method = method in {"snapshot", "full_qcow2", "incremental_qcow2", "raw"}
    if image_method and (not IMAGE_RE.fullmatch(image) or not SNAPSHOT_RE.fullmatch(snapshot)):
        raise HTTPException(400, "Tên image hoặc snapshot không hợp lệ")
    if method != "snapshot" and (not destination.startswith("/") or "\x00" in destination):
        raise HTTPException(400, "Đường dẫn backup phải là đường dẫn tuyệt đối trên management host")
    if method == "incremental_qcow2" and (not backing_file.startswith("/") or "\x00" in backing_file):
        raise HTTPException(400, "Incremental backup cần đường dẫn backing QCOW2 tuyệt đối")
    with db.SessionLocal() as session:
        cluster = _active_cluster(session, cluster_id)
        if not cluster:
            raise HTTPException(404, "Không tìm thấy cụm đang hoạt động")
        if method in {"metadata_etcd", "metadata_antietcd"} and not cluster.etcd_address:
            raise HTTPException(400, "Backup metadata cần Etcd address rõ ràng trong cấu hình cụm")
        params = {
            "method": method, "image": image, "snapshot": snapshot,
            "destination": destination, "backing_file": backing_file,
            "management_host": cluster.management_host, "ssh_user": cluster.ssh_user,
            "ssh_key_path": cluster.ssh_key_path, "etcd_address": cluster.etcd_address,
            "etcd_prefix": cluster.etcd_prefix, "config_path": cluster.config_path,
            "exec_mode": cluster.exec_mode, "container_name": cluster.container_name,
        }
        name = cluster.name
    labels = {
        "snapshot": "Snapshot CoW trong cụm", "full_qcow2": "Full QCOW2",
        "incremental_qcow2": "Incremental QCOW2 layer", "raw": "RAW image",
        "metadata_etcd": "Snapshot metadata etcd", "metadata_antietcd": "Dump metadata antietcd",
    }
    detail = f"Image: {image}@{snapshot}" if image_method else "Toàn bộ metadata key-value"
    target = "Giữ trong cụm (không phải off-cluster backup)" if method == "snapshot" else destination
    plan = f"BACKUP VITASTOR — {name}\nKiểu: {labels[method]}\n{detail}\nĐích: {target}\n\n1. Kiểm tra cụm HEALTHY và công cụ cần thiết\n2. {'Tạo snapshot live trước khi export' if image_method else 'Chụp metadata nhất quán'}\n3. {'Hoàn tất snapshot nội bộ' if method == 'snapshot' else 'Export và xác minh file có dữ liệu'}"
    return _create_operation("backup", name, params, plan, user, cluster_id)


@router.post("/operations/{operation_id}/execute")
async def execute_operation(operation_id: str, background: BackgroundTasks, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        # Claim with a compare-and-set so two concurrent approval requests
        # cannot both start the same destructive workflow.
        active_cluster = or_(
            VitastorOperation.cluster_id.is_(None),
            exists().where(
                VitastorCluster.id == VitastorOperation.cluster_id,
                VitastorCluster.is_active.is_(True),
            ),
        )
        claimed = session.query(VitastorOperation).filter(
            VitastorOperation.id == operation_id,
            VitastorOperation.status == "PENDING_APPROVAL",
            active_cluster,
        ).update(
            {VitastorOperation.status: "RUNNING", VitastorOperation.started_at: datetime.utcnow()},
            synchronize_session=False,
        )
        if claimed != 1:
            session.rollback()
            row = session.get(VitastorOperation, operation_id)
            if row and row.status == "PENDING_APPROVAL" and row.cluster_id:
                raise HTTPException(409, "Cụm Vitastor đã bị vô hiệu hoá hoặc xoá")
            raise HTTPException(409, "Thao tác không còn ở trạng thái chờ duyệt")
        session.commit()
    background.add_task(_execute, operation_id)
    return {"status": "RUNNING"}


@router.post("/operations/{operation_id}/reject")
async def reject_operation(operation_id: str, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        row = session.get(VitastorOperation, operation_id)
        if not row or row.status != "PENDING_APPROVAL": raise HTTPException(409, "Không thể từ chối thao tác")
        row.status, row.finished_at = "REJECTED", datetime.utcnow(); session.commit()
    return {"status": "REJECTED"}


@router.get("/operations/{operation_id}")
async def operation_status(operation_id: str, user: str = Depends(require_vitastor_login)):
    _require_admin(user)
    with db.SessionLocal() as session:
        row = session.get(VitastorOperation, operation_id)
        if not row: raise HTTPException(404, "Không tìm thấy thao tác")
        return {"id": row.id, "status": row.status, "progress": json.loads(row.progress_json), "error": row.error_message}
