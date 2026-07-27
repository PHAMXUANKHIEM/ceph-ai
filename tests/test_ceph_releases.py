from shared.ceph_releases import codename_for_version, major_version, next_min_version


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
    # reaching the repo-URL-building code, even though that code already
    # builds the repo URL from the exact version string (not the codename)
    # and works for any release download.ceph.com still hosts.
    assert codename_for_version("14.2.22") == "nautilus"


def test_codename_for_version_unknown_major_returns_none():
    assert codename_for_version("99.0.0") is None


def test_codename_for_version_unparseable_string_returns_none():
    assert codename_for_version("not-a-version") is None
