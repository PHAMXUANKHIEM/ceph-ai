"""The container rollout must not bypass backup-first migrations."""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_container_rollout_uses_backup_first_migration_entrypoint():
    script = (ROOT / "scripts/deploy/restart_container_stack.sh").read_text()
    artifact_check = script.index('if [ -n "$DEPLOY_IMAGE" ]; then')
    migration = script.index('"$REPO_DIR/scripts/deploy/run_migrations.sh"')
    restart = script.index("systemctl restart ceph-ai-containers.service")

    assert artifact_check < migration < restart
    assert '"$REPO_DIR/.venv/bin/alembic" upgrade heads' not in script


def test_migration_entrypoint_refuses_to_upgrade_without_backup():
    script = (ROOT / "scripts/deploy/run_migrations.sh").read_text()
    backup = script.index("backup_output=")
    backup_guard = script.index('if [[ -z "$backup_path" ]]')
    upgrade = script.index(".venv/bin/python -m alembic upgrade head")

    assert backup < backup_guard < upgrade
