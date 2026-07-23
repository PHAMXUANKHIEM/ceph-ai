"""Static Ceph release metadata — major version number -> release codename /
minimum next-release version. Used by BOTH watcher/ceph_client.py (suggesting
a next target version on the Upgrade page) and worker/executor/commands.py
(building a download.ceph.com repo URL for the ceph-deploy/package-based
upgrade path) — kept in shared/ rather than duplicated per-process (unlike
e.g. CHAT_REQUEST_CEPH_CODE, which IS deliberately duplicated across watcher/
dashboard to keep those processes' internals decoupled) because this is pure
static reference data with no process-boundary concerns, same posture as
watcher/ceph_client.py::VALID_EXEC_MODES already being process-agnostic data.

Not auto-updated from upstream (no internet access assumed) — extend
RELEASES as new Ceph releases ship.
"""

RELEASES: dict[int, dict[str, str]] = {
    15: {"codename": "octopus", "next_min_version": "16.2.0"},
    16: {"codename": "pacific", "next_min_version": "17.2.0"},
    17: {"codename": "quincy", "next_min_version": "18.2.0"},
    18: {"codename": "reef", "next_min_version": "19.2.0"},
    19: {"codename": "squid", "next_min_version": "20.2.0"},
    20: {"codename": "tentacle", "next_min_version": None},
}


def major_version(version: str) -> int | None:
    """Parses the leading major version number out of an `x.y.z` string —
    returns None (no guess) for anything unparseable, same fail-loud-not-
    silent posture as the rest of this codebase's version handling."""
    try:
        return int(version.split(".")[0])
    except (ValueError, IndexError):
        return None


def next_min_version(version: str) -> str | None:
    major = major_version(version)
    if major is None:
        return None
    release = RELEASES.get(major)
    return release["next_min_version"] if release else None


def codename_for_version(version: str) -> str | None:
    """Returns the Ceph release codename (e.g. "squid") a download.ceph.com
    repo URL needs for `version`'s major release — None if `version`'s major
    isn't in RELEASES (a release this table hasn't been updated for, or an
    unparseable string), so a caller can refuse rather than build a broken
    repo URL."""
    major = major_version(version)
    if major is None:
        return None
    release = RELEASES.get(major)
    return release["codename"] if release else None
