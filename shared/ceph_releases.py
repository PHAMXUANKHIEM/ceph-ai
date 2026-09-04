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
this list hasn't been extended for yet, or an internal-only build).

2026-08-06: replaced the old single `min_el_version` (one floor per MAJOR
release, e.g. "Pacific needs at least el8") with `el_history` (one
"which el majors does THIS EXACT point release support" timeline per
release) after a full live audit against download.ceph.com found the
coarser model was actively wrong, not just imprecise:
  - Octopus (15.x) was recorded as min_el_version=8 — WRONG, every single
    Octopus point release (15.2.0..15.2.17) actually shipped el7 AND el8
    RPMs (verified: rpm-15.2.0/el7/.../ceph-mon-15.2.0-0.el7.x86_64.rpm and
    rpm-15.2.17/el7/... both 200).
  - Reef (18.x) was recorded as min_el_version=9 — WRONG, the first three
    Reef point releases (18.2.0, 18.2.1, 18.2.2) shipped BOTH el8 and el9;
    el8 was only dropped starting 18.2.4.
  - Quincy (17.x) was recorded as a flat min_el_version=8 forever — this
    was the FLOOR of 17.2.0, but 17.2.8/17.2.9 dropped el8 entirely (el9
    only). A caller on CentOS 8 upgrading to 17.2.9 would have seen NO
    warning under the old floor-only model (8 >= 8 "looked" fine) and then
    hit a real 404 installing — the exact class of bug this rewrite closes.
The single per-major floor could only ever express "too old", never "this
later point release dropped support for an OS an earlier point release of
the SAME major used to support" — which is exactly what happened for
Quincy and (going the other direction, gained support later) Nautilus.
`el_history` fixes this by recording every observed transition, so
el_versions_for()/os_upgrade_warning() below check the EXACT target
version's real OS support, not just its major's first release.

Methodology (2026-08-06, all releases below): for every known point release
of every major in this table (91 versions total, Mimic through Tentacle),
probed https://download.ceph.com/rpm-<path>/el{7,8,9,10}/x86_64/
ceph-mon-<version>-0.el<N>.x86_64.rpm concurrently and recorded which el
majors returned 200 vs 404. Nautilus/Mimic used their codename path
(rpm-nautilus/, rpm-mimic/ — see `repo_path_uses_codename` below); every
other major used its own exact-version path (rpm-<version>/). One anomaly
found this way — Reef 18.2.3 — was cross-checked against debian-reef's
pool listing and quay.io's ceph/ceph image tags too (see its own note
below) since a same-day withdrawal is unusual enough to double-check
before trusting a single probe. Everything else here is exactly what the
probe returned, not inferred from release notes."""

RELEASES: dict[int, dict] = {
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
    #
    # `el_history`: [(version, (el majors supported FROM this version
    # onward, until the next entry)), ...], oldest first — see this
    # module's 2026-08-06 docstring note above for why this replaced a
    # single per-major floor, and the "Methodology" note for how every
    # entry below was obtained (live probe, not release notes).
    13: {
        "codename": "mimic",
        "next_min_version": "14.2.0",
        "versions": [f"13.2.{p}" for p in range(0, 11)],
        "repo_path_uses_codename": True,
        "el_history": [("13.2.0", (7,))],
    },
    14: {
        "codename": "nautilus",
        "next_min_version": "15.2.0",
        "versions": [f"14.2.{p}" for p in range(0, 23)],
        "repo_path_uses_codename": True,
        # el8 was NOT there from 14.2.0 — RHEL 8 shipped 2019-05-07,
        # after Nautilus's 2019-03-19 GA. el8 builds only start at 14.2.10
        # (verified: 14.2.0..14.2.9 el8 all 404, 14.2.10..14.2.22 el8 all
        # 200) — the mirror image of the Quincy el8-drop below (support
        # GAINED partway through the release instead of lost).
        "el_history": [("14.2.0", (7,)), ("14.2.10", (7, 8))],
    },
    15: {
        "codename": "octopus",
        "next_min_version": "16.2.0",
        "versions": [f"15.2.{p}" for p in range(0, 18)],
        # Every single Octopus point release (15.2.0..15.2.17) shipped
        # BOTH el7 and el8 — the previous table's min_el_version=8 for
        # this release was simply wrong (never independently live-checked
        # before 2026-08-06; see this module's docstring).
        "el_history": [("15.2.0", (7, 8))],
    },
    16: {
        "codename": "pacific",
        "next_min_version": "17.2.0",
        "versions": [f"16.2.{p}" for p in range(0, 16)],
        # Confirmed live 2026-08-05 by the operator who hit the CentOS 7
        # -> Pacific 16.2.15 case this whole el-tracking feature exists
        # for — Pacific never published el7 RPMs, el8 only, for every
        # point release (16.2.0..16.2.15 all el8-only, no el7/el9).
        "el_history": [("16.2.0", (8,))],
    },
    17: {
        "codename": "quincy",
        "next_min_version": "18.2.0",
        "versions": [f"17.2.{p}" for p in range(0, 10)],
        # The case the user flagged 2026-08-06: 17.2.0..17.2.3 shipped el8
        # only; el9 was ADDED at 17.2.4 (both el8+el9 for 17.2.4..17.2.7);
        # el8 was then DROPPED at 17.2.8 (17.2.8 and 17.2.9 are el9-only).
        # A CentOS 8 host upgrading straight to 17.2.9 will 404 — this is
        # exactly why el_history checks the EXACT target version now
        # instead of just Quincy's overall (now-stale) el8 floor.
        "el_history": [("17.2.0", (8,)), ("17.2.4", (8, 9)), ("17.2.8", (9,))],
    },
    18: {
        "codename": "reef",
        "next_min_version": "19.2.0",
        # 18.2.3 deliberately excluded — verified 2026-08-06 that it does
        # not exist ANYWHERE: rpm-18.2.3/ itself 404s (both el8 and el9),
        # debian-reef's pool has no 18.2.3 .deb (only ...-1focal/-1jammy
        # builds of every OTHER Reef version), and quay.io's ceph/ceph
        # image repo has no v18.2.3 tag at all (empty tag list). This
        # version number was tagged/announced and then withdrawn before
        # any artifact was ever published — do NOT add it back to fill
        # the apparent gap in the range below; it will never resolve to
        # anything installable by any method (package OR cephadm/
        # container) on any OS.
        "versions": [f"18.2.{p}" for p in range(0, 9) if p != 3],
        # 18.2.0..18.2.2 shipped BOTH el8 and el9 — the previous table's
        # min_el_version=9 for this release was wrong for these three
        # (never independently live-checked before 2026-08-06). el8 was
        # dropped starting 18.2.4 (18.2.3 itself never existed, see above).
        "el_history": [("18.2.0", (8, 9)), ("18.2.4", (9,))],
    },
    19: {
        "codename": "squid",
        "next_min_version": "20.2.0",
        "versions": ["19.2.0", "19.2.1", "19.2.2"],
        "el_history": [("19.2.0", (9,))],
    },
    20: {
        "codename": "tentacle",
        "next_min_version": None,
        # Tentacle maintenance releases currently published by Ceph. Keep
        # this picker list explicit so Deploy/Upgrade/Restore show the same
        # newest point release while the free-text field remains available
        # for a release published after this table is updated.
        "versions": ["20.2.0", "20.2.1", "20.2.2", "20.2.3", "20.2.4"],
        "el_history": [("20.2.0", (9,))],
    },
}

# OS `ID` values (from /etc/os-release) this module knows how to compare
# against `el_history` above — the RHEL-family distros that actually
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


def _version_tuple(version: str) -> tuple[int, ...]:
    """`"17.2.9"` -> `(17, 2, 9)`, for ordering against `el_history`
    breakpoints below. Non-numeric segments become -1 (sorts before any
    real point release) rather than raising — callers here only ever feed
    it strings that already passed `major_version()`'s own parse."""
    parts = []
    for p in version.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(-1)
    return tuple(parts)


def el_versions_for(version: str) -> tuple[int, ...] | None:
    """The exact "el" (RHEL-family) major OS versions THIS SPECIFIC x.y.z
    Ceph release has packages for — e.g. `el_versions_for("17.2.9")` ->
    `(9,)` (el9 only — Quincy dropped el8 at 17.2.8), but
    `el_versions_for("17.2.0")` -> `(8,)` (el8 only — el9 wasn't added
    until 17.2.4). None if `version`'s major isn't in RELEASES. See
    RELEASES' own `el_history` comments for the live-verification behind
    every entry. Walks `el_history` for the version's major release and
    returns the entry in effect at `version` (the last breakpoint at or
    before it) — a version older than the release's very first tracked
    breakpoint falls back to that first entry rather than returning None,
    same "best known answer, not a hard reject" posture as the rest of
    this module."""
    major = major_version(version)
    if major is None:
        return None
    release = RELEASES.get(major)
    if release is None:
        return None
    v = _version_tuple(version)
    result = release["el_history"][0][1]
    for from_version, els in release["el_history"]:
        if v >= _version_tuple(from_version):
            result = els
        else:
            break
    return result


def next_min_version(version: str) -> str | None:
    major = major_version(version)
    if major is None:
        return None
    release = RELEASES.get(major)
    return release["next_min_version"] if release else None


def min_el_version_for(version: str) -> int | None:
    """The lowest "el" (RHEL-family) major OS version THIS SPECIFIC x.y.z
    Ceph release has packages for (see `el_versions_for` above — this is
    just `min()` of that). None if `version`'s major isn't in RELEASES."""
    els = el_versions_for(version)
    return min(els) if els else None


def min_os_label_for(version: str) -> str | None:
    """Human-friendly minimum-OS label for `version`'s own `el_versions_for`
    floor (e.g. "CentOS/RHEL/Rocky Linux/AlmaLinux 8 trở lên") — factored
    out of os_upgrade_warning()'s inline string so Story 11.1's OS Upgrade
    Gate screen (dashboard/routes/upgrade.py) can show it as its own field
    instead of re-parsing the full warning sentence. None if `version`'s
    major isn't in RELEASES (same as min_el_version_for)."""
    floor = min_el_version_for(version)
    if floor is None:
        return None
    return f"CentOS/RHEL/Rocky Linux/AlmaLinux {floor} trở lên"


def os_upgrade_warning(target_version: str, os_id: str, os_version_id: str) -> str | None:
    """Returns a Vietnamese warning if the OS described by `os_id`/
    `os_version_id` (as read from /etc/os-release, e.g. os_id="centos",
    os_version_id="7") does not have packages for `target_version` EXACTLY
    (via `el_versions_for` — not just `target_version`'s major's oldest
    point release). Covers both directions: the OS is too old for this
    exact point release (below its floor — the original CentOS 7 ->
    Pacific case this function was built for), AND the OS was DROPPED by
    a later point release of the same major that used to support it (the
    CentOS 8 -> Quincy 17.2.9 case — 17.2.0 supported el8, 17.2.9 no
    longer does). Returns None (no warning) when: the OS is compatible
    with this exact version; `os_id` isn't a tracked el-family distro (see
    EL_FAMILY_OS_IDS' own comment — Debian/Ubuntu/Fedora aren't modeled,
    so this simply says nothing rather than guessing); `os_version_id`
    isn't a parseable integer-leading string; or `target_version`'s major
    has no known history. Callers should treat None as "no warning to
    show", not "confirmed compatible" — see this function's own caveats
    above for what it does NOT catch."""
    if os_id not in EL_FAMILY_OS_IDS:
        return None
    try:
        os_major = int(os_version_id.split(".")[0])
    except (ValueError, AttributeError, IndexError):
        return None
    els = el_versions_for(target_version)
    if els is None or os_major in els:
        return None
    codename = (codename_for_version(target_version) or "").capitalize()
    floor = min(els)
    if os_major < floor:
        return (
            f"hệ điều hành hiện tại là {os_id} {os_version_id} (el{os_major}), nhưng Ceph "
            f"{target_version} ({codename}) yêu cầu tối thiểu el{floor} ({min_os_label_for(target_version)}) "
            f"— download.ceph.com không có gói cho phiên bản này trên "
            f"el{os_major}, cài đặt sẽ thất bại. Cần nâng cấp hệ điều hành TRƯỚC khi nâng cấp Ceph."
        )
    supported_label = ", ".join(f"el{e}" for e in els)
    return (
        f"hệ điều hành hiện tại là {os_id} {os_version_id} (el{os_major}), nhưng Ceph "
        f"{target_version} ({codename}) đã KHÔNG CÒN gói cho el{os_major} ở đúng bản này — bản này chỉ còn "
        f"gói cho {supported_label} (một bản {codename} CŨ HƠN có thể từng hỗ trợ el{os_major}, nhưng bản "
        f"{target_version} thì không). download.ceph.com không có gói cho phiên bản này trên el{os_major}, "
        f"cài đặt sẽ thất bại. Chọn một bản {codename} khác đã build cho el{os_major} (nếu có), hoặc nâng "
        f"cấp hệ điều hành lên {supported_label} trước khi tiếp tục."
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
    `_build_ceph_package_repo_command` docstring). Nautilus/Mimic are the
    releases where that per-version path never existed at all (see this
    module's `repo_path_uses_codename` comment above) — for them, and only
    them, this returns the codename instead. Callers must still call
    `codename_for_version(version) is None` themselves first to reject an
    unrecognized version — this function has no such guard and just
    echoes `version` back unchanged for anything not in RELEASES."""
    major = major_version(version)
    release = RELEASES.get(major) if major is not None else None
    if release is not None and release.get("repo_path_uses_codename"):
        return release["codename"]
    return version
