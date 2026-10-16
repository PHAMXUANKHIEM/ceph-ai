"""Customer install profile (deploy/customer/compose.yaml, scripts/customer/, plan R8.1)."""

import io
import json
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts.customer import build_env

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ROOT / "deploy" / "customer" / "compose.yaml"
SCRIPT = ROOT / "scripts" / "customer" / "ceph-ai-customer.sh"
SECRETS = {"db_password": "d" * 48, "rabbitmq_password": "r" * 48, "session_secret": "s" * 64,
           "dashboard_password_hash": "$2b$12$abcdefghijklmnopqrstuuvwxyzABCDEFGHIJKLMNOPQRSTUVWX"}


def test_the_new_env_keeps_known_settings_and_owns_the_stack_secrets():
    old = {"CEPH_MON_NODES": "10.2.0.11,10.2.0.12", "SSH_USER": "root", "TELEGRAM_INCIDENT_CHAT_ID": "-100",
           "DATABASE_URL": "postgresql://old@db:31081/x", "AUTOPILOT_ENABLED": "true",
           "TELEGRAM_FEDERATED_DATABASE_URLS": "postgresql://old", "NOT_A_SETTING": "x"}

    values, report = build_env.build(old, SECRETS, build_env.allowed_keys())

    assert values["CEPH_MON_NODES"] == "10.2.0.11,10.2.0.12" and values["SSH_USER"] == "root"
    assert values["DATABASE_URL"] == f"postgresql+psycopg://ceph_ai:{'d' * 48}@postgres:5432/ceph_ai"
    assert values["RABBITMQ_URL"] == f"amqp://ceph_ai:{'r' * 48}@rabbitmq:5672/"
    assert values["AUTOPILOT_ENABLED"] == "false" and values["DASHBOARD_USERNAME"] == "admin"
    assert "TELEGRAM_FEDERATED_DATABASE_URLS" not in values and "NOT_A_SETTING" not in values
    assert report["dropped_unknown"] == ["NOT_A_SETTING"]
    assert report["dropped_on_purpose"] == ["TELEGRAM_FEDERATED_DATABASE_URLS"]
    assert report["replaced"] == ["AUTOPILOT_ENABLED", "DATABASE_URL"]
    assert "d" * 48 not in json.dumps(report)


@pytest.mark.parametrize("secrets", [{}, {**SECRETS, "db_password": ""}])
def test_missing_secrets_are_refused(secrets):
    with pytest.raises(ValueError, match="missing secrets"):
        build_env.build({}, secrets, build_env.allowed_keys())


def test_a_value_with_a_line_break_is_refused():
    with pytest.raises(ValueError, match="line break"):
        build_env.build({"SSH_USER": "root\nEVIL=1"}, SECRETS, build_env.allowed_keys())


def test_the_written_file_is_unquoted_and_not_world_readable(tmp_path, monkeypatch):
    old = tmp_path / "old.env"
    old.write_text("SSH_USER=ceph\nCEPH_MON_NODES='10.2.0.11'\n")
    out = tmp_path / "config" / ".env"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(SECRETS)))

    assert build_env.main(["--out", str(out), "--old-env", str(old)]) == 0

    text = out.read_text()
    assert "CEPH_MON_NODES=10.2.0.11\n" in text and "SSH_USER=ceph\n" in text
    assert stat.S_IMODE(out.stat().st_mode) == 0o640


def test_the_generated_file_loads_as_settings(tmp_path, monkeypatch):
    out = tmp_path / ".env"
    values, _ = build_env.build({"SSH_USER": "ceph"}, SECRETS, build_env.allowed_keys())
    build_env.write(out, values)
    from config.settings import Settings
    settings = Settings(_env_file=str(out))
    assert settings.ssh_user == "ceph" and settings.autopilot_enabled is False


def _compose():
    return yaml.safe_load(COMPOSE.read_text())


def test_the_customer_stack_never_mounts_main_host_paths_or_publishes_backends():
    services = _compose()["services"]
    assert set(services) == {"postgres", "rabbitmq", "dashboard-web", "watcher", "remediation-watcher", "worker"}
    for name, service in services.items():
        for volume in service.get("volumes", []):
            assert not volume.startswith(("/root", "./")), (name, volume)
        if name != "dashboard-web":
            assert "ports" not in service, name
    assert services["dashboard-web"]["ports"] == ["${CEPH_AI_DASHBOARD_BIND:-0.0.0.0:8000}:8000"]
    for name in ("dashboard-web", "watcher", "remediation-watcher", "worker"):
        assert services[name]["image"].startswith("${CEPH_AI_IMAGE:?"), name
        assert services[name]["user"] == "10001:10001" and services[name]["read_only"] is True
    assert services["worker"]["environment"]["SSH_KEY_PATH"] == "/run/ceph-ai/credentials/ssh/id_ed25519"


def test_the_installer_is_valid_bash_and_never_sources_an_env_file():
    subprocess.run(["bash", "-n", str(SCRIPT)], check=True)
    text = SCRIPT.read_text()
    assert "source " not in text and "\n. " not in text
    assert "ALLOW_UNVERIFIED" not in text


# --- CNI compatibility (Podman 3.4 on Ubuntu 22.04, 10.2.0.94 on 15/10/2026) ------------------

from scripts.customer import cni_compat  # noqa: E402

PODMAN34_CONFLIST = {"cniVersion": "1.0.0", "name": "ceph-ai-customer_default", "plugins": [
    {"type": "bridge"}, {"type": "portmap"}, {"type": "firewall"}, {"type": "tuning"}, {"type": "dnsname"}]}
OLD_PLUGINS = ["0.1.0", "0.2.0", "0.3.0", "0.3.1", "0.4.0"]


def test_a_version_the_plugins_do_not_speak_is_lowered_to_their_highest_common_one():
    assert cni_compat.compatible_version(PODMAN34_CONFLIST, lambda plugin: set(OLD_PLUGINS)) == "0.4.0"


def test_a_supported_version_is_left_alone():
    assert cni_compat.compatible_version(PODMAN34_CONFLIST, lambda plugin: {*OLD_PLUGINS, "1.0.0"}) is None


def test_no_common_version_is_an_error():
    versions = {"bridge": {"0.4.0"}, "portmap": {"1.0.0"}}
    with pytest.raises(ValueError, match="no CNI version"):
        cni_compat.compatible_version({"cniVersion": "1.0.0", "plugins": [{"type": "bridge"}, {"type": "portmap"}]},
                                      versions.__getitem__)


def test_the_tool_asks_the_real_plugins_and_rewrites_the_file(tmp_path):
    plugins = tmp_path / "cni"
    plugins.mkdir()
    for name in ("bridge", "portmap", "firewall", "tuning", "dnsname"):
        script = plugins / name
        script.write_text("#!/bin/sh\n[ \"$CNI_COMMAND\" = VERSION ] || exit 1\n"
                          f"echo '{json.dumps({'cniVersion': '0.4.0', 'supportedVersions': OLD_PLUGINS})}'\n")
        script.chmod(0o755)
    conflist = tmp_path / "ceph-ai-customer_default.conflist"
    conflist.write_text(json.dumps(PODMAN34_CONFLIST))
    conflist.chmod(0o644)

    assert cni_compat.main([str(conflist), "--plugin-dir", str(plugins)]) == 0
    assert json.loads(conflist.read_text())["cniVersion"] == "0.4.0"
    assert stat.S_IMODE(conflist.stat().st_mode) == 0o644
    assert cni_compat.main([str(conflist), "--plugin-dir", str(plugins)]) == 0  # second run: nothing to do


def test_a_missing_plugin_is_reported(tmp_path):
    conflist = tmp_path / "net.conflist"
    conflist.write_text(json.dumps(PODMAN34_CONFLIST))
    assert cni_compat.main([str(conflist), "--plugin-dir", str(tmp_path / "none")]) == 2


def test_the_installer_prepares_the_network_before_any_container_starts():
    text = SCRIPT.read_text()
    backends = text.index("start_backends() {")
    assert text.index("  ensure_network\n", backends) < text.index("compose up -d postgres rabbitmq", backends)
    assert "cni_compat.py" in text


def test_postgres_18_keeps_its_data_layout_and_a_traversable_host_dir():
    # 10.2.0.94, 15/10/2026: mounting at /var/lib/postgresql/data with a 0700 host dir failed
    # with "mkdir: cannot create directory '/var/lib/postgresql/data': Permission denied".
    postgres = _compose()["services"]["postgres"]
    assert postgres["volumes"] == ["/var/lib/ceph-ai-postgres:/var/lib/postgresql:rw"]
    assert "PGDATA" not in postgres.get("environment", {})
    text = SCRIPT.read_text()
    assert "install -d -m 0755 /var/lib/ceph-ai-postgres /var/lib/ceph-ai-rabbitmq" in text
    assert "0700" not in next(line for line in text.splitlines() if "/var/lib/ceph-ai-postgres" in line)


def test_podman_34_accepts_the_security_options():
    # Podman 3.4: 'invalid --security-opt 1: "no-new-privileges:true"' (10.2.0.94, 15/10/2026).
    for name, service in _compose()["services"].items():
        for option in service.get("security_opt", []):
            assert ":true" not in option, (name, option)


def test_the_runtime_dir_exists_before_any_container_and_the_install_is_marked_done_only_after_smoke():
    text = SCRIPT.read_text()
    install = text[text.index("cmd_install() {"):]
    assert install.index("  runtime_dir\n") < install.index("cmd_install_start\n")
    start = text[text.index("cmd_install_start() {"):]
    assert start.index("  smoke\n") < start.index("current-image-ref") < start.index("  install_unit\n")
    resume = install[:install.index("  preflight\n")]
    assert "compose down" in resume  # an unfinished run's Dashboard must not hold the port at preflight


def test_backups_never_stream_a_dump_through_podman_exec():
    # Podman 3.4 truncated a dump streamed through `podman exec` to 106496 bytes (15/10/2026).
    backup = SCRIPT.read_text().split("cmd_backup() {", 1)[1].split("\n}\n", 1)[0]
    assert "pg_dump -U ceph_ai -Fc ceph_ai -f" in backup and 'podman cp "$pg:$inside"' in backup
    assert "> \"$file.tmp\"" not in backup and "< \"$file" not in backup


def test_the_stack_never_shares_host_dirs_with_an_older_install():
    # 10.2.0.94, 16/10/2026: a root-run Ceph AI from 05/09 keeps writing root-owned files into
    # /var/lib/ceph-ai/cache and /run/ceph-ai; the watchers (uid 10001) crash-looped on them.
    compose = _compose()
    app = compose["services"]["watcher"]
    assert "/var/lib/ceph-ai-customer:/var/lib/ceph-ai:rw" in app["volumes"]
    assert "/run/ceph-ai-customer:/run/ceph-ai:rw" in app["volumes"]
    for name, service in compose["services"].items():
        for volume in service.get("volumes", []):
            assert not volume.startswith(("/var/lib/ceph-ai:", "/run/ceph-ai:")), (name, volume)
        assert service.get("env_file") in (None, "/var/lib/ceph-ai-customer/config/.env") or name in ("postgres", "rabbitmq")
    text = SCRIPT.read_text()
    assert "DATA=/var/lib/ceph-ai-customer" in text and "RUN_DIR=/run/ceph-ai-customer" in text
    for command in ("cmd_install_start() {", "cmd_upgrade() {", "cmd_start() {"):
        body = text.split(command, 1)[1].split("\n}\n", 1)[0]
        assert "data_ownership" in body, command
