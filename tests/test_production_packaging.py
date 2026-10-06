"""Static regressions for the immutable production artifact path."""

from pathlib import Path
import tomllib

import yaml


ROOT = Path(__file__).resolve().parents[1]


def test_default_runtime_has_no_source_mount_or_code_repair():
    compose = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert "code-repair" not in compose["services"]
    for name, service in compose["services"].items():
        assert service["image"] == "${CEPH_AI_IMAGE:-ceph-ai:local}", name
        volumes = service.get("volumes", [])
        assert not any(item.startswith("./:/app:") for item in volumes), name
        assert not any(item.endswith(":/app:rw") for item in volumes), name


def test_dockerfile_pins_base_and_hash_locked_dependencies():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "FROM docker.io/library/python:3.11-slim@sha256:" in dockerfile
    assert "LABEL org.opencontainers.image.revision=$GIT_COMMIT" in dockerfile
    assert "--require-hashes -r requirements-prod.lock" in dockerfile
    assert "--no-deps --no-build-isolation ." in dockerfile
    assert "COPY dashboard ./dashboard" in dockerfile
    assert "COPY docs/ceph-ai-rca-knowledge.md" in dockerfile
    assert "COPY alembic ./alembic" in dockerfile
    lock = (ROOT / "requirements-prod.lock").read_text(encoding="utf-8")
    assert "--hash=sha256:" in lock
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for requirement in project["project"]["dependencies"]:
        package, version = requirement.split("==", 1)
        normalized = package.replace("_", "-").lower()
        assert f"{normalized}=={version}" in lock, requirement


def test_release_pushes_scanned_artifact_and_deploys_digest_only():
    workflow = (ROOT / ".github/workflows/ci-cd.yml").read_text(encoding="utf-8")
    assert workflow.index("- name: Scan image for blocking vulnerabilities") < workflow.index(
        "- name: Push the scanned image and record its registry digest"
    )
    assert "registry-image-ref.txt" in workflow
    assert "packages: write" in workflow
    deploy = (ROOT / "scripts/deploy/restart_container_stack.sh").read_text(encoding="utf-8")
    assert 'podman pull "$DEPLOY_IMAGE"' in deploy
    assert 'podman-compose build' not in deploy
    assert 'approved_image_id="$(podman image inspect "$DEPLOY_IMAGE" --format' in deploy
    assert '"org.opencontainers.image.revision"' in deploy
    assert 'mv -f "$image_ref_tmp" "$IMAGE_REF_FILE"' in deploy
    launcher = (ROOT / "container-up").read_text(encoding="utf-8")
    assert "release-artifacts/current-image-ref" in launcher
    assert "services=(dashboard-web telegram-ai full-executor watcher remediation-watcher worker vault-monitor)" in launcher
    repair_unit = (ROOT / "scripts/deploy/systemd/ceph-ai-code-repair-supervisor.service").read_text()
    assert "Environment=CODE_REPAIR_PUSH=false" in repair_unit
    assert "Environment=CODE_REPAIR_DEPLOY_STAGING=false" in repair_unit
    assert "Environment=CODE_REPAIR_PROMOTE_MAIN=false" in repair_unit
    assert "systemctl enable --now ceph-ai-code-repair-supervisor.service" in deploy


def test_rollback_requires_explicit_schema_compatibility_acknowledgement():
    script = (ROOT / "scripts/deploy/rollback_container_stack.sh").read_text(encoding="utf-8")
    assert "CEPH_AI_ROLLBACK_ACK_COMPATIBLE_SCHEMA" in script
    assert 'podman pull "$previous_ref"' in script
    assert "alembic downgrade" not in script


def test_production_migration_runs_from_approved_image():
    script = (ROOT / "scripts/deploy/run_migrations.sh").read_text(encoding="utf-8")
    assert '"$CEPH_AI_IMAGE" python -m alembic upgrade head' in script
    assert '"$CEPH_AI_IMAGE" python -m alembic heads' in script
    assert 'if [ "$image_head" != "$head_revision" ]; then' in script
    deploy = (ROOT / "scripts/deploy/restart_container_stack.sh").read_text(encoding="utf-8")
    assert 'export CEPH_AI_IMAGE="$DEPLOY_IMAGE"' in deploy


def test_a_reboot_brings_rabbitmq_back_before_the_stack():
    """RabbitMQ is a long-lived container outside Compose with no restart
    policy; after a reboot nothing started it (plan SM1)."""
    launcher = (ROOT / "container-up").read_text(encoding="utf-8")
    start = launcher.index("podman start rabbitmq")
    wait = launcher.index("rabbitmq-diagnostics -q ping")
    compose = launcher.index("podman-compose up")
    assert start < wait < compose
    assert "exit 5" in launcher[wait:compose]  # no queue -> do not start Watcher/Worker

    systemd = ROOT / "scripts" / "deploy" / "systemd"
    rabbitmq = (systemd / "ceph-ai-rabbitmq.service").read_text(encoding="utf-8")
    stack = (systemd / "ceph-ai-containers.service").read_text(encoding="utf-8")
    assert "ExecStart=/usr/bin/podman start rabbitmq" in rabbitmq
    assert "Before=ceph-ai-containers.service" in rabbitmq
    assert "WantedBy=multi-user.target" in rabbitmq
    assert "After=network-online.target ceph-ai-rabbitmq.service" in stack
    assert "Wants=network-online.target ceph-ai-rabbitmq.service" in stack
    assert "ExecStart=/root/ceph-ai/container-up" in stack

    deploy = (ROOT / "scripts" / "deploy" / "restart_container_stack.sh").read_text(encoding="utf-8")
    assert "systemd/ceph-ai-rabbitmq.service\" /etc/systemd/system/" in deploy
    assert "systemd/ceph-ai-containers.service\" /etc/systemd/system/" in deploy
    assert "systemctl enable ceph-ai-rabbitmq.service ceph-ai-containers.service" in deploy
