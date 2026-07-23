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


def test_codename_for_version_unknown_major_returns_none():
    assert codename_for_version("99.0.0") is None


def test_codename_for_version_unparseable_string_returns_none():
    assert codename_for_version("not-a-version") is None
