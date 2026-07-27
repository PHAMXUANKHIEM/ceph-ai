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

2026-07-27: added `versions` (known x.y.z point releases, oldest first) to
each entry — powers the Deploy/Upgrade pages' "chọn dòng release rồi chọn
phiên bản" two-step picker (dashboard/routes/deploy_cluster.py,
dashboard/routes/upgrade.py). Same "not auto-updated, extend by hand"
posture as the table itself: each list is only as complete as it was when
last edited, NOT queried live from download.ceph.com — a point release
shipped after this was last updated just won't show up as a dropdown
option yet. This is a convenience picker only, never the sole way to enter
a version: every version input this feeds also stays a free-text field a
caller can type any x.y.z into directly (e.g. a very new point release
this list hasn't been extended for yet, or an internal-only build)."""

RELEASES: dict[int, dict[str, str]] = {
    # `repo_path_uses_codename`: verified live against download.ceph.com,
    # 2026-07-27 — Nautilus is the ONE release in this table that was NEVER
    # published under a per-exact-version directory at all (rpm-14.2.22/el8/
    # -> 404, debian-14.2.22/ -> 404 too); only the rolling
    # rpm-nautilus/debian-nautilus codename alias ever existed for it (both
    # confirmed 200 on el7/el8). Every later release (Octopus+) DOES have
    # real per-version directories, which repo_path_version() below prefers
    # for them (see worker/executor/cluster_deploy.py's
    # _build_ceph_package_repo_command docstring for why: the codename alias
    # is a ROLLING pointer that can silently drop an older OS's packages
    # once a later point release stops shipping them). Nautilus has no such
    # risk going forward — it EOL'd 2021-06-29 (per download.ceph.com's own
    # debian-nautilus/dists/ listing) and will never get another point
    # release, so its codename alias is permanently frozen at 14.2.22 and
    # safe to reference by codename forever. Every other entry below omits
    # this key (defaults to False via `.get()`).
    14: {
        "codename": "nautilus",
        "next_min_version": "15.2.0",
        "versions": [f"14.2.{p}" for p in range(0, 23)],
        "repo_path_uses_codename": True,
    },
    15: {
        "codename": "octopus",
        "next_min_version": "16.2.0",
        "versions": [f"15.2.{p}" for p in range(0, 18)],
    },
    16: {
        "codename": "pacific",
        "next_min_version": "17.2.0",
        "versions": [f"16.2.{p}" for p in range(0, 16)],
    },
    17: {
        "codename": "quincy",
        "next_min_version": "18.2.0",
        "versions": [f"17.2.{p}" for p in range(0, 9)],
    },
    18: {
        "codename": "reef",
        "next_min_version": "19.2.0",
        "versions": [f"18.2.{p}" for p in range(0, 9)],
    },
    19: {
        "codename": "squid",
        "next_min_version": "20.2.0",
        "versions": ["19.2.0", "19.2.1", "19.2.2"],
    },
    20: {
        "codename": "tentacle",
        "next_min_version": None,
        "versions": ["20.2.0"],
    },
}


def codenames_oldest_first() -> list[tuple[str, str]]:
    """[(codename, "codename (major)")] for every known release, oldest
    major version first — feeds the Deploy/Upgrade pages' release-line
    dropdown (step 1 of the two-step version picker)."""
    return [
        (release["codename"], f"{release['codename'].capitalize()} ({major}.x)")
        for major, release in sorted(RELEASES.items())
    ]


def versions_by_codename() -> dict[str, list[str]]:
    """{codename: [known point releases, oldest first]} for every known
    release — feeds step 2 of the picker once a codename is chosen in step
    1. A plain dict (not filtered to one codename) so the whole thing can
    be embedded once as JSON in the page and switched between client-side
    with no extra request per codename picked."""
    return {release["codename"]: release["versions"] for release in RELEASES.values()}


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


def repo_path_version(version: str) -> str:
    """The path segment (`rpm-<this>/`, `debian-<this>/`) to actually use
    when building a download.ceph.com repo URL for `version`. Normally just
    `version` itself unchanged — every caller building these URLs already
    deliberately prefers the exact-version path over the codename's rolling
    alias for that reason (see worker/executor/cluster_deploy.py's
    `_build_ceph_package_repo_command` docstring). Nautilus is the one
    release where that per-version path never existed at all (see this
    module's `repo_path_uses_codename` comment above) — for it, and only
    it, this returns the codename instead. Callers must still call
    `codename_for_version(version) is None` themselves first to reject an
    unrecognized version — this function has no such guard and just
    echoes `version` back unchanged for anything not in RELEASES."""
    major = major_version(version)
    release = RELEASES.get(major) if major is not None else None
    if release is not None and release.get("repo_path_uses_codename"):
        return release["codename"]
    return version
