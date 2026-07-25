import os
import stat
from pathlib import Path

# Same file every process (Dashboard, Worker, Watcher) reads its settings
# from at startup (config/settings.py's pydantic-settings `Settings()`) —
# this module is the single place that WRITES to it. Originally lived only
# in dashboard/routes/settings.py; extracted here (Story 8.1) so Worker-side
# code (worker/executor/cluster_deploy.py) can persist a newly-deployed
# cluster's connection config without importing a Dashboard route module
# (Worker must never import from dashboard/ — AD-3's layering applies the
# same direction to this as it does to SSH execution itself).
ENV_PATH = Path(__file__).resolve().parent.parent / ".env"

# config/settings.py field name -> .env variable name, for every field the
# "Cấu hình cụm" form on the Settings page edits. Moved here (alongside
# ENV_PATH) rather than staying dashboard-route-local so
# worker/executor/cluster_deploy.py can build the same field->env-var-name
# payload after a successful "Dựng cụm" deploy without importing a
# Dashboard route module (Worker must never import from dashboard/ — AD-3's
# layering direction). `dashboard/routes/settings.py` imports this dict
# rather than defining its own — single source of truth.
#
# ssh_key_path is deliberately NOT here — it's not an editable field on the
# cluster form (one key, set once via .env/server config, not something
# that changes per Ceph cluster the way MON/OSD nodes do); callers that
# need to change it read/write settings.ssh_key_path directly instead.
CLUSTER_ENV_NAMES: dict[str, str] = {
    "ceph_mon_nodes": "CEPH_MON_NODES",
    "ceph_mon_hostnames": "CEPH_MON_HOSTNAMES",
    "ceph_container_name": "CEPH_CONTAINER_NAME",
    "ceph_osd_nodes": "CEPH_OSD_NODES",
    "ceph_osd_container_name": "CEPH_OSD_CONTAINER_NAME",
    "ceph_mgr_nodes": "CEPH_MGR_NODES",
    "ceph_rgw_nodes": "CEPH_RGW_NODES",
    "ceph_rgw_container_name": "CEPH_RGW_CONTAINER_NAME",
    "ceph_exec_mode": "CEPH_EXEC_MODE",
    "ssh_user": "SSH_USER",
}


def apply_env_updates(existing_lines: list[str], fields: dict[str, str]) -> list[str]:
    for name, value in fields.items():
        # Review Story 5.1: an unchecked embedded newline in a submitted
        # field would inject an arbitrary extra line into .env (e.g.
        # overwriting DASHBOARD_PASSWORD_HASH/SESSION_SECRET_KEY) — reject
        # at the single insertion point so every caller is protected.
        if "\n" in value or "\r" in value:
            raise ValueError(f"{name}: giá trị không được chứa ký tự xuống dòng")

    remaining = dict(fields)
    new_lines = []
    for line in existing_lines:
        matched_name = next((name for name in remaining if line.startswith(f"{name}=")), None)
        if matched_name is not None:
            new_lines.append(f"{matched_name}={remaining.pop(matched_name)}")
        else:
            new_lines.append(line)
    for name, value in remaining.items():
        new_lines.append(f"{name}={value}")
    return new_lines


def write_env_lines(new_lines: list[str]) -> None:
    """Writes to a temp file then os.replace()s over the original — never a
    window where .env is truncated/partially written if the process dies
    mid-write (.env also holds DASHBOARD_PASSWORD_HASH/SESSION_SECRET_KEY).
    Restricts the file to owner-only read/write afterward — this file holds
    multiple secrets."""
    tmp_path = ENV_PATH.with_suffix(".tmp")
    tmp_path.write_text("\n".join(new_lines) + "\n")
    os.chmod(tmp_path, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp_path, ENV_PATH)


def update_env_file(env_var_name: str, value: str) -> None:
    """Replace the line `env_var_name=...` in .env if present, else append it."""
    existing_lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    write_env_lines(apply_env_updates(existing_lines, {env_var_name: value}))


def update_env_file_batch(fields: dict[str, str]) -> None:
    """Same atomicity guarantee as `update_env_file`, but for several fields
    written together in ONE read-modify-write-replace cycle (Review Story
    5.1). Writing the whole batch as a single temp-file-then-replace makes
    it all-or-nothing — no window where .env has a mix of old and new field
    values."""
    existing_lines = ENV_PATH.read_text().splitlines() if ENV_PATH.exists() else []
    write_env_lines(apply_env_updates(existing_lines, fields))
