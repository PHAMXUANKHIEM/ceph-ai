"""Create the container-owned settings file without executing `.env`."""

import os
import secrets
import subprocess
import re
from urllib.parse import quote
from pathlib import Path

from dotenv import dotenv_values, set_key, unset_key

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".env"
TARGET = Path("/var/lib/ceph-ai/config/.env")
SECRETS_DIR = Path("/etc/ceph-ai-secrets")
EXECUTOR_TOKEN_FILE = SECRETS_DIR / "single-full-executor.token"
RABBITMQ_PASSWORD_FILE = SECRETS_DIR / "rabbitmq-ceph-ai.password"
DUAL_AGENT_UID = "10001"
DUAL_AGENT_WRITE_PATHS = (
    "config", "dashboard", "shared", "watcher", "worker", "tests", "vitastor",
)
DUAL_AGENT_ACCOUNT_PATHS = (".codex-account", ".ai-accounts")


def _write_executor_token() -> None:
    SECRETS_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    if EXECUTOR_TOKEN_FILE.exists():
        return
    temporary = EXECUTOR_TOKEN_FILE.with_suffix(".tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as handle:
        handle.write(secrets.token_urlsafe(32) + "\n")
    os.replace(temporary, EXECUTOR_TOKEN_FILE)


def _read_or_create_secret(path: Path) -> str:
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    temporary = path.with_suffix(".tmp")
    fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    value = secrets.token_urlsafe(32)
    with os.fdopen(fd, "w") as handle:
        handle.write(value + "\n")
    os.replace(temporary, path)
    return value


def _configure_container_rabbitmq(values: dict) -> None:
    """Use a non-guest broker account when app containers cross the bridge."""
    rabbit_url = str(values.get("RABBITMQ_URL") or "")
    if not rabbit_url or not re.search(r"@(localhost|host\.containers\.internal)(?::\d+)?(?:/|$)", rabbit_url):
        return
    password = _read_or_create_secret(RABBITMQ_PASSWORD_FILE)
    username = "ceph_ai"
    users = subprocess.run(
        ["podman", "exec", "rabbitmq", "rabbitmqctl", "list_users"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=True,
    ).stdout.splitlines()
    if not any(line.split() and line.split()[0] == username for line in users):
        subprocess.run(["podman", "exec", "rabbitmq", "rabbitmqctl", "add_user", username, password], check=True)
    subprocess.run(
        ["podman", "exec", "rabbitmq", "rabbitmqctl", "set_permissions", "-p", "/", username, ".*", ".*", ".*"],
        check=True,
    )
    set_key(
        TARGET, "RABBITMQ_URL",
        f"amqp://{username}:{quote(password, safe='')}@host.containers.internal:5672/",
        # podman-compose passes env_file entries through literally; quoted
        # values become a malformed URL inside the container.
        quote_mode="never",
    )


def _remove_legacy_executor_token(path: Path) -> None:
    if path.exists():
        unset_key(path, "SINGLE_FULL_EXECUTOR_TOKEN")


def _grant_dual_agent_workspace_access() -> None:
    """Grant the unprivileged /dual subprocess source access, never secrets."""
    # CLI binaries are mounted below /root/.local. Traversal alone is enough
    # for that immutable tooling path; it does not expose root-owned files.
    subprocess.run(["setfacl", "-m", f"u:{DUAL_AGENT_UID}:x", "/root"], check=True)
    subprocess.run(["setfacl", "-m", f"u:{DUAL_AGENT_UID}:rx", str(ROOT)], check=True)
    for relative in DUAL_AGENT_WRITE_PATHS + DUAL_AGENT_ACCOUNT_PATHS:
        path = ROOT / relative
        if not path.exists():
            continue
        subprocess.run(["setfacl", "-Rm", f"u:{DUAL_AGENT_UID}:rwX", str(path)], check=True)
        for directory in (item for item in path.rglob("*") if item.is_dir()):
            subprocess.run(
                ["setfacl", "-m", f"d:u:{DUAL_AGENT_UID}:rwX", str(directory)], check=True,
            )
        if path.is_dir():
            subprocess.run(
                ["setfacl", "-m", f"d:u:{DUAL_AGENT_UID}:rwX", str(path)], check=True,
            )


def main() -> None:
    TARGET.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not TARGET.exists():
        TARGET.write_text(SOURCE.read_text())
        TARGET.chmod(0o600)
    # Rotate away the old shared-env token. The new token is only mounted in
    # telegram-ai and full-executor, never injected into generic app config.
    _remove_legacy_executor_token(SOURCE)
    _remove_legacy_executor_token(TARGET)
    _write_executor_token()
    _grant_dual_agent_workspace_access()
    values = dotenv_values(TARGET)
    _configure_container_rabbitmq(values)


if __name__ == "__main__":
    main()
