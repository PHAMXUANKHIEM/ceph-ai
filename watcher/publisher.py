import json

import aio_pika

from shared.mq import QUEUE_NAME, declare_topology, get_connection

SCHEMA_VERSION = "1.0"


def build_envelope(
    incident_id: str,
    ceph_code: str,
    detected_at: str,
    nodes: list[str],
    log_excerpt: str,
    cluster_snapshot: dict,
    cluster_id: str | None = None,
    ssh_user: str = "",
    ssh_key_path: str = "",
    ceph_exec_mode: str = "",
    ceph_container_name: str = "",
    osd_hosts: dict[int, str] | None = None,
) -> dict:
    """Build the Incident message envelope (matches Story 1.4 AC #2 exactly).

    `cluster_id` (multi-cluster observability Phase 1, default None): tags
    which cluster this Incident belongs to — None means "the default
    cluster", same COALESCE convention as Incident.cluster_id/
    WatcherHeartbeat.cluster_id.

    `ssh_user`/`ssh_key_path`/`ceph_exec_mode`/`ceph_container_name`
    (2026-08-10, multi-tenant remediation Phase 1): the ORIGINATING
    cluster's own credentials/exec-mode, captured at Incident-detection time
    so `worker/main.py`'s diagnosis/remediation pipeline executes against
    the RIGHT cluster regardless of which one it is — Worker never re-derives
    these from its own global `settings` singleton for a non-default
    cluster's Incident. Every caller must pass real values (there is no
    sensible "unset" default that's still safe to execute against — an
    empty string here would surface as a clear, loud SSH/paramiko failure
    rather than silently falling back to the default cluster's creds).

    `osd_hosts` (2026-08-20): {osd_id: host} đã TRA THẬT cho những OSD mà
    check này nêu đích danh — worker/llm/router_client.py đưa thẳng vào
    prompt để model không phải suy ra osd nào nằm ở máy nào từ một danh
    sách node phẳng (nó từng suy sai, xem watcher/osd_hosts.py). Rỗng/None
    nghĩa là "chưa xác định được", KHÔNG phải "không có OSD nào liên
    quan" — prompt phải nói đúng sự khác biệt đó."""
    return {
        "schema_version": SCHEMA_VERSION,
        "incident_id": incident_id,
        "ceph_code": ceph_code,
        "detected_at": detected_at,
        "nodes": nodes,
        # Khoá JSON phải là chuỗi — dict[int, str] đi qua json.dumps sẽ tự
        # đổi khoá thành chuỗi, làm ở đây cho envelope nhất quán hai đầu.
        "osd_hosts": {str(k): v for k, v in (osd_hosts or {}).items()},
        "log_excerpt": log_excerpt,
        "cluster_snapshot": cluster_snapshot,
        "cluster_id": cluster_id,
        "ssh_user": ssh_user,
        "ssh_key_path": ssh_key_path,
        "ceph_exec_mode": ceph_exec_mode,
        "ceph_container_name": ceph_container_name,
    }


async def publish_incident(envelope: dict) -> None:
    """Publish an Incident envelope to the `incidents` queue.

    Topology (queue/exchange names, DLX) is owned solely by `shared/mq.py`
    (AD-2) — this function only declares it (idempotent) and publishes,
    never redefines it.
    """
    connection = await get_connection()
    async with connection:
        channel = await connection.channel()
        await declare_topology(channel)
        await channel.default_exchange.publish(
            aio_pika.Message(
                body=json.dumps(envelope).encode(),
                delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
            ),
            routing_key=QUEUE_NAME,
        )
