import bcrypt

from dashboard.cluster_authorization import (
    CAPABILITY_APPROVE,
    CAPABILITY_READ,
    has_cluster_capability,
)
from shared import db
from shared.models import Cluster, ClusterCapabilityGrant, User
from shared.models import SingleFullAudit
from shared.single_full_audit import finish_run, start_run


def _create_operator(username: str) -> User:
    with db.SessionLocal() as session:
        user = User(
            username=username,
            password_hash=bcrypt.hashpw(b"operator-password", bcrypt.gensalt()).decode(),
            is_admin=False,
            is_active=True,
            created_by="admin",
        )
        session.add(user)
        session.commit()
        session.refresh(user)
        return user


def test_regular_user_cannot_approve_without_grant(dashboard_client):
    user = _create_operator("rbac-no-grant")
    with db.SessionLocal() as session:
        cluster_id = session.query(ClusterCapabilityGrant.cluster_id).first()
        if cluster_id is None:
            from shared.clusters import ensure_default_cluster
            cluster_id = ensure_default_cluster(session).id
        else:
            cluster_id = cluster_id[0]
    assert not has_cluster_capability(user.username, cluster_id, CAPABILITY_APPROVE)


def test_grant_and_revoke_are_cluster_specific(dashboard_client):
    user = _create_operator("rbac-scoped")
    with db.SessionLocal() as session:
        from shared.clusters import ensure_default_cluster
        cluster_id = ensure_default_cluster(session).id
        other_cluster = Cluster(
            name="RBAC-other-cluster",
            ceph_mon_nodes="10.20.1.251",
            ssh_user="root",
            ssh_key_path="/tmp/rbac-test-key",
        )
        session.add(other_cluster)
        session.flush()
        session.add(ClusterCapabilityGrant(
            user_id=user.id, cluster_id=cluster_id,
            capability=CAPABILITY_APPROVE, granted_by="admin",
        ))
        session.commit()
        other_cluster_id = other_cluster.id

    assert has_cluster_capability(user.username, cluster_id, CAPABILITY_READ)
    assert has_cluster_capability(user.username, cluster_id, CAPABILITY_APPROVE)
    assert not has_cluster_capability(user.username, other_cluster_id, CAPABILITY_READ)

    with db.SessionLocal() as session:
        grant = session.query(ClusterCapabilityGrant).filter_by(
            user_id=user.id, cluster_id=cluster_id, capability=CAPABILITY_APPROVE,
        ).one()
        grant.is_active = False
        session.commit()
    assert not has_cluster_capability(user.username, cluster_id, CAPABILITY_APPROVE)


def test_admin_keeps_global_cluster_access(dashboard_client):
    with db.SessionLocal() as session:
        from shared.clusters import ensure_default_cluster
        cluster_id = ensure_default_cluster(session).id
    assert has_cluster_capability("admin", cluster_id, CAPABILITY_READ)
    assert has_cluster_capability("admin", cluster_id, CAPABILITY_APPROVE)


def test_single_full_audit_stores_fingerprint_and_terminal_result(dashboard_client):
    with db.SessionLocal() as session:
        from shared.clusters import ensure_default_cluster
        cluster_id = ensure_default_cluster(session).id
    start_run(
        run_id="audit-test-run",
        actor="telegram-chat:123",
        chat_id="123",
        cluster_id=cluster_id,
        cluster_ref=f"default:{cluster_id}",
        prompt="do not persist this secret-like prompt",
    )
    with db.SessionLocal() as session:
        row = session.query(SingleFullAudit).filter_by(run_id="audit-test-run").one()
        assert row.status == "RUNNING"
        assert row.prompt_sha256 != "do not persist this secret-like prompt"
        assert row.operator_acknowledged is True
    finish_run("audit-test-run", status="SUCCEEDED", result_code="completed")
    with db.SessionLocal() as session:
        row = session.query(SingleFullAudit).filter_by(run_id="audit-test-run").one()
        assert row.status == "SUCCEEDED"
        assert row.finished_at is not None
