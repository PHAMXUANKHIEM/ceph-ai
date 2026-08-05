from shared.ceph_releases import (
    codename_for_version,
    major_version,
    min_el_version_for,
    min_os_label_for,
    next_min_version,
    os_upgrade_warning,
    repo_path_version,
)


def test_major_version_parses_leading_number():
    assert major_version("18.2.4") == 18


def test_major_version_returns_none_for_unparseable_string():
    assert major_version("not-a-version") is None
    assert major_version("") is None


def test_next_min_version_known_major():
    assert next_min_version("17.2.7") == "18.2.0"
    assert next_min_version("18.2.4") == "19.2.0"


def test_next_min_version_unknown_major_returns_none():
    assert next_min_version("99.0.0") is None


def test_next_min_version_latest_known_release_returns_none():
    # Tentacle (20) is the newest release this table knows about — no next
    # release to suggest yet, must not fabricate one.
    assert next_min_version("20.1.0") is None


def test_codename_for_version_known_major():
    assert codename_for_version("18.2.4") == "reef"
    assert codename_for_version("19.2.0") == "squid"
    assert codename_for_version("17.2.7") == "quincy"
    # Nautilus (14) — added 2026-07-27: this table originally started at
    # Octopus (15), so any 14.2.x version (e.g. the real final Nautilus
    # point release, 14.2.22) was rejected as "unrecognized" before ever
    # reaching the repo-URL-building code. That code's repo PATH still
    # needed its own separate fix, though — see test_repo_path_version_*
    # below and repo_path_version()'s docstring: unlike every later
    # release, Nautilus was never published under a per-exact-version
    # directory at all, only the codename alias.
    assert codename_for_version("14.2.22") == "nautilus"


def test_codename_for_version_unknown_major_returns_none():
    assert codename_for_version("99.0.0") is None


def test_codename_for_version_unparseable_string_returns_none():
    assert codename_for_version("not-a-version") is None


def test_repo_path_version_uses_exact_version_for_normal_releases():
    assert repo_path_version("18.2.4") == "18.2.4"
    assert repo_path_version("19.2.0") == "19.2.0"
    assert repo_path_version("17.2.7") == "17.2.7"


def test_repo_path_version_uses_codename_for_nautilus():
    # Verified live against download.ceph.com, 2026-07-27: rpm-14.2.22/el8/
    # and debian-14.2.22/ both 404 — Nautilus was only ever published under
    # the rolling rpm-nautilus/debian-nautilus codename alias, which is now
    # permanently frozen since Nautilus is EOL (no future point release can
    # ever change what it points to).
    assert repo_path_version("14.2.22") == "nautilus"
    assert repo_path_version("14.2.0") == "nautilus"


def test_repo_path_version_unknown_major_returns_version_unchanged():
    assert repo_path_version("99.0.0") == "99.0.0"


# --- Story 11.1 (OS Upgrade Gate): min_el_version_for/os_upgrade_warning/
# min_os_label_for had ZERO test coverage before this story, despite
# already gating real proposals in dashboard/routes/upgrade.py since
# 2026-08-05 — filling that pre-existing gap alongside the Gate-screen work
# that consumes these functions. ---------------------------------------


def test_min_el_version_for_known_major():
    assert min_el_version_for("14.2.22") == 7  # Nautilus — last el7-capable release
    assert min_el_version_for("16.2.15") == 8  # Pacific — the CentOS 7 case this exists for
    assert min_el_version_for("19.2.0") == 9


def test_min_el_version_for_unknown_major_returns_none():
    assert min_el_version_for("99.0.0") is None


def test_min_os_label_for_known_major():
    assert min_os_label_for("16.2.15") == "CentOS/RHEL/Rocky Linux/AlmaLinux 8 trở lên"


def test_min_os_label_for_unknown_major_returns_none():
    assert min_os_label_for("99.0.0") is None


def test_os_upgrade_warning_centos7_below_pacific_floor():
    warning = os_upgrade_warning("16.2.15", "centos", "7")
    assert warning is not None
    assert "centos 7" in warning
    assert "el7" in warning
    assert "el8" in warning
    assert "CentOS/RHEL/Rocky Linux/AlmaLinux 8 trở lên" in warning


def test_os_upgrade_warning_none_when_os_meets_floor():
    assert os_upgrade_warning("16.2.15", "centos", "8") is None
    assert os_upgrade_warning("16.2.15", "rocky", "9") is None


def test_os_upgrade_warning_none_for_non_el_family_os():
    # Debian/Ubuntu/Fedora aren't modeled (EL_FAMILY_OS_IDS' own comment) —
    # must say nothing rather than guessing.
    assert os_upgrade_warning("16.2.15", "ubuntu", "20.04") is None
    assert os_upgrade_warning("16.2.15", "fedora", "30") is None


def test_os_upgrade_warning_none_for_unparseable_version_id():
    assert os_upgrade_warning("16.2.15", "centos", "not-a-version") is None


def test_os_upgrade_warning_none_for_unknown_target_major():
    assert os_upgrade_warning("99.0.0", "centos", "7") is None
