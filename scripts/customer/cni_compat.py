"""Make a CNI network config usable by the host's CNI plugins (customer hosts, Podman 3.4).

Ubuntu 22.04 ships Podman 3.4.4, which writes network configs with
``"cniVersion": "1.0.0"``, and containernetworking-plugins 0.9.1, whose plugins
only speak up to 0.4.0. Every container on that network then fails with
``plugin firewall does not support config version "1.0.0"`` (10.2.0.94, 15/10/2026).

    python3 cni_compat.py /etc/cni/net.d/<network>.conflist [--plugin-dir DIR ...]

Asks each plugin the conflist uses for its supported versions (CNI_COMMAND=VERSION)
and, when the file's version is not supported by all of them, rewrites it to the
highest version they all support. Standard library only: runs on the host.
"""

from __future__ import annotations

import argparse
import json
import os
# subprocess only runs CNI plugins from fixed system directories (plugin_versions).
import subprocess  # nosec B404
import sys
from pathlib import Path

DEFAULT_PLUGIN_DIRS = ("/usr/lib/cni", "/opt/cni/bin", "/usr/libexec/cni")


def _version_key(version: str) -> tuple[int, ...]:
    return tuple(int(part) for part in version.split("."))


def plugin_versions(plugin: str, plugin_dirs) -> set[str]:
    for directory in plugin_dirs:
        path = Path(directory) / plugin
        if os.access(path, os.X_OK):
            # argv list, no shell; the binary comes from a fixed plugin directory.
            result = subprocess.run(  # nosec B603
                [str(path)], input="", capture_output=True, text=True, timeout=10,
                env={"CNI_COMMAND": "VERSION", "PATH": os.environ.get("PATH", "")})
            return set(json.loads(result.stdout or "{}").get("supportedVersions") or [])
    raise FileNotFoundError(f"CNI plugin {plugin!r} not found in {', '.join(plugin_dirs)}")


def compatible_version(config: dict, versions_of) -> str | None:
    """None when the file's version already works; else the version to write."""
    plugins = [item["type"] for item in config.get("plugins", []) if item.get("type")]
    if not plugins:
        return None
    common = set.intersection(*(versions_of(plugin) for plugin in plugins))
    if config.get("cniVersion") in common:
        return None
    if not common:
        raise ValueError(f"no CNI version is supported by all of {', '.join(plugins)}")
    return max(common, key=_version_key)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("conflist", type=Path)
    parser.add_argument("--plugin-dir", action="append", dest="plugin_dirs")
    args = parser.parse_args(argv)
    plugin_dirs = args.plugin_dirs or DEFAULT_PLUGIN_DIRS
    config = json.loads(args.conflist.read_text(encoding="utf-8"))
    try:
        target = compatible_version(config, lambda plugin: plugin_versions(plugin, plugin_dirs))
    except (FileNotFoundError, ValueError) as exc:
        print(f"cni_compat: {exc}", file=sys.stderr)
        return 2
    if target is None:
        print(f"cni_compat: {args.conflist.name} cniVersion {config.get('cniVersion')} OK")
        return 0
    old = config.get("cniVersion")
    config["cniVersion"] = target
    temporary = args.conflist.with_name(f".{args.conflist.name}.tmp")
    temporary.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    os.chmod(temporary, args.conflist.stat().st_mode & 0o777)
    os.replace(temporary, args.conflist)
    print(f"cni_compat: {args.conflist.name} cniVersion {old} -> {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
