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
    # 2026-07-27 (Nautilus) / 2026-08-06 (Mimic) — these are the TWO
    # releases in this table that were NEVER published under a
    # per-exact-version directory at all (rpm-14.2.22/el8/ -> 404,
    # debian-14.2.22/ -> 404; rpm-13.2.10/el7/ -> 404 too); only the rolling
    # rpm-nautilus/debian-nautilus and rpm-mimic/debian-mimic codename
    # aliases ever existed for them (rpm-mimic/el7 confirmed 200; rpm-14.2.x
    # confirmed 200 on el7/el8). Every later release (Octopus+) DOES have
    # real per-version directories, which repo_path_version() below prefers
    # for them (see worker/executor/cluster_deploy.py's
    # _build_ceph_package_repo_command docstring for why: the codename alias
    # is a ROLLING pointer that can silently drop an older OS's packages
    # once a later point release stops shipping them). Nautilus and Mimic
    # have no such risk going forward — both are long EOL (Nautilus
    # 2021-06-29, Mimic 2020-07-22, per download.ceph.com's own
    # debian-<codename>/dists/ listings) and will never get another point
    # release, so their codename aliases are permanently frozen (at
    # 14.2.22 and 13.2.10 respectively) and safe to reference by codename
    # forever. Every other entry below omits this key (defaults to False
    # via `.get()`).
    # 2026-08-05: `min_el_version` added — the lowest RHEL/CentOS/Rocky/
    # AlmaLinux ("el") major OS version any packaged build of that release
    # EVER shipped for, e.g. Pacific (16.x) never published el7 RPMs, so
    # its floor is 8. Powers shared.ceph_releases.os_upgrade_warning()
    # below, used by dashboard/routes/upgrade.py's package-based upgrade
    # proposal to warn an operator BEFORE approving an upgrade that would
    # otherwise 404 fetching download.ceph.com/rpm-<version>/el<N>/ at
    # execution time (worker/executor/commands.py's own
    # _upgrade_ceph_cluster_package_download_command only discovers that
    # failure live, mid-run, on the target host itself).
    #
    # Same "hand-curated, not queried live" posture as the rest of this
    # table: Nautilus/Pacific values confirmed directly by the operator
    # (2026-08-05) who hit this exact CentOS 7 -> Pacific 16.2.15 case;
    # Mimic's min_el_version=7 (and its versions list) IS independently
    # live-verified against download.ceph.com (2026-08-06: rpm-mimic/el7 ->
    # 200, rpm-mimic/el8 -> 404, and every ceph-mon-13.2.{0..10} RPM under
    # rpm-mimic/el7/x86_64/ -> 200 while 13.2.11 -> 404 — no el8 build of
    # Mimic was ever published, matching Mimic predating RHEL 8's 2019-05
    # release); Octopus/Quincy/Reef/Squid/Tentacle are from public Ceph
    # release notes' documented OS support matrix, not independently
    # live-verified here — flag it if one of these turns out wrong so this
    # table gets corrected. Only tracks the FLOOR for a release's very
    # first point release, same granularity gap this module already
    # discloses elsewhere for `repo_path_uses_codename`/the Quincy
    # el8-dropped-later case in worker/executor/commands.py — a later
    # point release narrowing support further than its own first release's
    # floor is NOT modeled and will under-warn (say "OK") for that
    # narrower case.
    13: {
        "codename": "mimic",
        "next_min_version": "14.2.0",
        "versions": [f"13.2.{p}" for p in range(0, 11)],
        "repo_path_uses_codename": True,
        "min_el_version": 7,
    },
    14: {
        "codename": "nautilus",
        "next_min_version": "15.2.0",
        "versions": [f"14.2.{p}" for p in range(0, 23)],
        "repo_path_uses_codename": True,
        "min_el_version": 7,
    },
    15: {
        "codename": "octopus",
        "next_min_version": "16.2.0",
        "versions": [f"15.2.{p}" for p in range(0, 18)],
        "min_el_version": 8,
    },
    16: {
        "codename": "pacific",
        "next_min_version": "17.2.0",
        "versions": [f"16.2.{p}" for p in range(0, 16)],
        "min_el_version": 8,
    },
    17: {
        "codename": "quincy",
        "next_min_version": "18.2.0",
        "versions": [f"17.2.{p}" for p in range(0, 9)],
        "min_el_version": 8,
    },
    18: {
        "codename": "reef",
        "next_min_version": "19.2.0",
        "versions": [f"18.2.{p}" for p in range(0, 9)],
        "min_el_version": 9,
    },
    19: {
        "codename": "squid",
        "next_min_version": "20.2.0",
        "versions": ["19.2.0", "19.2.1", "19.2.2"],
        "min_el_version": 9,
    },
    20: {
        "codename": "tentacle",
        "next_min_version": None,
        "versions": ["20.2.0"],
        "min_el_version": 9,
    },
}

# OS `ID` values (from /etc/os-release) this module knows how to compare
# against `min_el_version` above — the RHEL-family distros that actually
# use "elN" packaging (matches worker/executor/cluster_deploy.py's own
# rpm-family list minus "fedora": Fedora's own VERSION_ID isn't an "el"
# equivalent — download.ceph.com never published a Fedora-specific repo,
# and this app's own repo-URL builder always targets `rpm -E %rhel`, which
# is undefined on real Fedora — so warning off Fedora's VERSION_ID would be
# comparing the wrong scale entirely; deliberately left unmodeled here, a
# pre-existing gap in the upgrade command builder itself, not something
# this check can fix).  Debian/Ubuntu aren't modeled either (no equivalent
# min-version table curated yet) — os_upgrade_warning() below simply
# returns None (no warning) for any os_id not in this set.
EL_FAMILY_OS_IDS = {"rhel", "centos", "rocky", "almalinux"}


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


def min_el_version_for(version: str) -> int | None:
    """The lowest "el" (RHEL-family) major OS version `version`'s major
    Ceph release ever shipped RPM builds for — see RELEASES' own
    `min_el_version` comment above for sourcing/caveats. None if
    `version`'s major isn't in RELEASES."""
    major = major_version(version)
    if major is None:
        return None
    release = RELEASES.get(major)
    return release.get("min_el_version") if release else None


def min_os_label_for(version: str) -> str | None:
    """Human-friendly minimum-OS label for `version`'s major release's
    `min_el_version` floor (e.g. "CentOS/RHEL/Rocky Linux/AlmaLinux 8 trở
    lên") — factored out of os_upgrade_warning()'s inline string so Story
    11.1's OS Upgrade Gate screen (dashboard/routes/upgrade.py) can show it
    as its own field instead of re-parsing the full warning sentence. None
    if `version`'s major isn't in RELEASES (same as min_el_version_for)."""
    floor = min_el_version_for(version)
    if floor is None:
        return None
    return f"CentOS/RHEL/Rocky Linux/AlmaLinux {floor} trở lên"


def os_upgrade_warning(target_version: str, os_id: str, os_version_id: str) -> str | None:
    """Returns a Vietnamese warning if the OS described by `os_id`/
    `os_version_id` (as read from /etc/os-release, e.g. os_id="centos",
    os_version_id="7") is below `target_version`'s known `min_el_version`
    floor — the CentOS 7 -> Ceph Pacific case this function exists for.
    Returns None (no warning) when: the OS is compatible; `os_id` isn't a
    tracked el-family distro (see EL_FAMILY_OS_IDS' own comment — Debian/
    Ubuntu/Fedora aren't modeled, so this simply says nothing rather than
    guessing); `os_version_id` isn't a parseable integer-leading string; or
    `target_version`'s major has no known floor. Callers should treat None
    as "no warning to show", not "confirmed compatible" — see this
    function's own caveats above for what it does NOT catch."""
    if os_id not in EL_FAMILY_OS_IDS:
        return None
    try:
        os_major = int(os_version_id.split(".")[0])
    except (ValueError, AttributeError, IndexError):
        return None
    floor = min_el_version_for(target_version)
    if floor is None or os_major >= floor:
        return None
    codename = (codename_for_version(target_version) or "").capitalize()
    return (
        f"hệ điều hành hiện tại là {os_id} {os_version_id} (el{os_major}), nhưng Ceph "
        f"{target_version} ({codename}) yêu cầu tối thiểu el{floor} ({min_os_label_for(target_version)}) "
        f"— download.ceph.com không có gói cho phiên bản này trên "
        f"el{os_major}, cài đặt sẽ thất bại. Cần nâng cấp hệ điều hành TRƯỚC khi nâng cấp Ceph."
    )


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
