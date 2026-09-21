"""Create the container-owned settings file without executing `.env`."""

import os
import secrets
import shutil
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
DUAL_WORKSPACE = Path("/var/lib/ceph-ai/dual-workspace")
FULL_EXECUTOR_UID = "10001"
FULL_EXECUTOR_ACCOUNT_ROOT = Path("/var/lib/ceph-ai/full-executor-accounts")
FULL_EXECUTOR_SSH_ROOT = Path("/var/lib/ceph-ai/full-executor-ssh")
FULL_EXECUTOR_SECRET_ROOT = Path("/var/lib/ceph-ai/full-executor-secrets")
RUNTIME_UID = "10001"
RUNTIME_ROOT = Path("/var/lib/ceph-ai")
RUNTIME_DIR = Path("/run/ceph-ai")
RUNTIME_STATE_FILES = frozenset(
    {
        "ceph-capability-learning.json",
        "dual-ai-execution.lock",
        "telegram-chat-clusters.json",
        "telegram-chat-modes.json",
        "telegram-single-full-confirmations.json",
        "telegram-single-full-runs.json",
        "telegram-update-offsets.json",
    }
)
DUAL_AGENT_ACCOUNT_PATHS = (".codex-account", ".claude-account", ".ai-accounts")
LEGACY_DUAL_AGENT_WRITE_PATHS = (
    "config", "dashboard", "shared", "watcher", "worker", "tests", "vitastor",
)


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
    resource_pattern = r"^(incidents|incidents\.dlx|incidents\.dlq|ai\.delegated\.tasks|ai\.delegated\.tasks\.exchange|ai\.delegated\.tasks\.dlx|ai\.delegated\.tasks\.dlq)$"
    subprocess.run(
        ["podman", "exec", "rabbitmq", "rabbitmqctl", "set_permissions", "-p", "/", username,
         resource_pattern, resource_pattern, resource_pattern],
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
    """Grant /dual write access only to its isolated candidate workspace."""
    # CLI binaries are mounted below /root/.local. Traversal alone is enough
    # for that immutable tooling path; it does not expose root-owned files.
    subprocess.run(["setfacl", "-m", f"u:{DUAL_AGENT_UID}:x", "/root"], check=True)
    subprocess.run(["setfacl", "-m", f"u:{DUAL_AGENT_UID}:rx", str(ROOT)], check=True)
    for relative in DUAL_AGENT_ACCOUNT_PATHS:
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


def _revoke_legacy_dual_source_access() -> None:
    """Remove ACLs from the former live-source Dual write path."""
    for relative in LEGACY_DUAL_AGENT_WRITE_PATHS:
        path = ROOT / relative
        if not path.exists():
            continue
        subprocess.run(["setfacl", "-R", "-x", f"u:{DUAL_AGENT_UID}", str(path)], check=True)
        for directory in (item for item in path.rglob("*") if item.is_dir()):
            subprocess.run(
                ["setfacl", "-x", f"d:u:{DUAL_AGENT_UID}", str(directory)], check=False,
            )
        if path.is_dir():
            subprocess.run(["setfacl", "-x", f"d:u:{DUAL_AGENT_UID}", str(path)], check=False)


def _ensure_dual_workspace() -> None:
    """Create the non-deployed checkout that Telegram /dual may modify.

    It is deliberately an independent clone, not a Git worktree: a worktree
    shares the main repository metadata and lets an untrusted writer influence
    the checkout used by privileged services.
    """
    if not DUAL_WORKSPACE.exists():
        DUAL_WORKSPACE.parent.mkdir(mode=0o750, parents=True, exist_ok=True)
        subprocess.run(
            ["git", "clone", "--no-local", str(ROOT), str(DUAL_WORKSPACE)], check=True,
        )
    # This script runs as root, but the clone is chown'd to DUAL_AGENT_UID
    # below on every run (including this check on the *next* run) — so from
    # the second bootstrap onward, root's git sees an owner mismatch and
    # refuses as "dubious ownership" unless explicitly told this exact path
    # is expected to be owned by someone else.
    result = subprocess.run(
        ["git", "-c", f"safe.directory={DUAL_WORKSPACE}", "-C", str(DUAL_WORKSPACE),
         "rev-parse", "--is-inside-work-tree"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, check=False,
    )
    if result.returncode != 0 or result.stdout.strip() != "true":
        raise RuntimeError(f"Dual workspace is not a valid Git repository: {DUAL_WORKSPACE}")
    subprocess.run(["chown", "-R", f"{DUAL_AGENT_UID}:{DUAL_AGENT_UID}", str(DUAL_WORKSPACE)], check=True)


def _chown_tree(path: Path, *, uid: str) -> None:
    """Give one service account ownership of its dedicated secret tree."""
    for item in (path, *path.rglob("*")):
        os.chown(item, int(uid), int(uid))


def _copy_account_tree(source: Path, target: Path) -> None:
    """Copy provider auth once into a uid-isolated, read-only container mount."""
    if not source.is_dir():
        target.mkdir(mode=0o700, parents=True, exist_ok=True)
    elif not target.exists():
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns("tmp", "shell_snapshots"),
        )
    if target.exists():
        _chown_tree(target, uid=FULL_EXECUTOR_UID)
        target.chmod(0o700)


def _copy_secret_file(source: Path, target: Path) -> None:
    """Install a private SSH key without exposing the host's root path."""
    if not source.is_file():
        return
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = target.with_suffix(f".{os.getpid()}.tmp")
    shutil.copyfile(source, temporary)
    os.chown(temporary, int(FULL_EXECUTOR_UID), int(FULL_EXECUTOR_UID))
    os.chmod(temporary, 0o600 if not source.name.endswith(".pub") else 0o644)
    os.replace(temporary, target)


def _ensure_full_executor_credentials(values: dict) -> None:
    """Provision only the credentials needed by the non-root executor."""
    FULL_EXECUTOR_ACCOUNT_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(FULL_EXECUTOR_ACCOUNT_ROOT, int(FULL_EXECUTOR_UID), int(FULL_EXECUTOR_UID))
    FULL_EXECUTOR_ACCOUNT_ROOT.chmod(0o700)
    FULL_EXECUTOR_SSH_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(FULL_EXECUTOR_SSH_ROOT, int(FULL_EXECUTOR_UID), int(FULL_EXECUTOR_UID))
    FULL_EXECUTOR_SSH_ROOT.chmod(0o700)
    FULL_EXECUTOR_SECRET_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chown(FULL_EXECUTOR_SECRET_ROOT, int(FULL_EXECUTOR_UID), int(FULL_EXECUTOR_UID))
    FULL_EXECUTOR_SECRET_ROOT.chmod(0o700)
    _copy_secret_file(EXECUTOR_TOKEN_FILE, FULL_EXECUTOR_SECRET_ROOT / "token")
    _copy_account_tree(ROOT / ".codex-account", FULL_EXECUTOR_ACCOUNT_ROOT / "codex")
    _copy_account_tree(ROOT / ".claude-account", FULL_EXECUTOR_ACCOUNT_ROOT / "claude")
    ssh_key = str(values.get("SSH_KEY_PATH") or "").strip()
    if ssh_key:
        source = Path(ssh_key).expanduser()
        _copy_secret_file(source, FULL_EXECUTOR_SSH_ROOT / "id_ed25519")
        _copy_secret_file(Path(f"{source}.pub"), FULL_EXECUTOR_SSH_ROOT / "id_ed25519.pub")


def _grant_runtime_service_access() -> None:
    """Prepare the shared non-root app UID's narrow state boundaries."""
    subprocess.run(["setfacl", "-m", f"u:{RUNTIME_UID}:x", str(RUNTIME_ROOT)], check=True)
    RUNTIME_DIR.mkdir(mode=0o750, parents=True, exist_ok=True)
    subprocess.run(["setfacl", "-Rm", f"u:{RUNTIME_UID}:rwX,m::rwX", str(RUNTIME_DIR)], check=True)
    config_dir = TARGET.parent
    config_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    subprocess.run(["setfacl", "-m", f"u:{RUNTIME_UID}:rwx", str(config_dir)], check=True)
    if TARGET.exists():
        subprocess.run(["setfacl", "-m", f"u:{RUNTIME_UID}:rw", str(TARGET)], check=True)
    for path in RUNTIME_ROOT.iterdir():
        if path.is_file() and path.name in RUNTIME_STATE_FILES:
            subprocess.run(["setfacl", "-m", f"u:{RUNTIME_UID}:rw,m::rw", str(path)], check=True)
        elif path.is_file():
            subprocess.run(["setfacl", "-x", f"u:{RUNTIME_UID}", str(path)], check=False)
    for relative in ("cache", "ai-tasks", "backups", "backups_missing", "release-artifacts", "ssh"):
        path = RUNTIME_ROOT / relative
        path.mkdir(mode=0o750, parents=True, exist_ok=True)
        subprocess.run(["setfacl", "-Rm", f"u:{RUNTIME_UID}:rwX,m::rwX", str(path)], check=True)


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
    _ensure_dual_workspace()
    _ensure_full_executor_credentials(dotenv_values(TARGET))
    _revoke_legacy_dual_source_access()
    _grant_dual_agent_workspace_access()
    values = dotenv_values(TARGET)
    _configure_container_rabbitmq(values)
    # The RabbitMQ bootstrap may atomically replace TARGET; apply runtime ACLs
    # last so the non-root services retain access to the current file.
    _grant_runtime_service_access()


if __name__ == "__main__":
    main()
