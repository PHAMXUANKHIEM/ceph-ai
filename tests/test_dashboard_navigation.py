from pathlib import Path


TEMPLATE_DIR = Path("dashboard/templates")
APP_JS = Path("dashboard/static/app.js")

SHARED_NAV_PATHS = {
    "/",
    "/nodes",
    "/volumes",
    "/bucket-access-log",
    "/openstack/auth-pool",
    "/deploy-cluster",
    "/delete-cluster",
    "/upgrade",
    "/patch",
    "/convert-cluster",
    "/backups",
    "/restore-cluster",
    "/settings",
}


def test_every_ceph_shell_template_loads_the_shared_navigation_script():
    missing = []
    for template in sorted(TEMPLATE_DIR.glob("*.html")):
        source = template.read_text(encoding="utf-8")
        if 'class="main-nav"' in source and "/static/app.js" not in source:
            missing.append(template.name)

    assert missing == []


def test_shared_shell_does_not_inject_redundant_generic_page_heading():
    source = APP_JS.read_text(encoding="utf-8")

    assert "CEPH AI · CONTROL PLANE" not in source
    assert "Giám sát, phân tích và vận hành hạ tầng Ceph" not in source
    assert 'className = "page-heading"' not in source


def test_shared_navigation_seeds_every_non_permission_gated_group():
    source = APP_JS.read_text(encoding="utf-8")

    for path in SHARED_NAV_PATHS:
        assert f'["{path}",' in source, f"shared navigation does not seed {path}"

    assert '"/object-storage/user-settings"' in source
    assert 'paths: ["/object-storage/buckets", "/object-storage/users", "/object-storage/user-settings", "/bucket-access-log"]' in source


def test_permission_gated_links_are_not_synthesized_by_shared_navigation():
    source = APP_JS.read_text(encoding="utf-8")
    shared_block = source.split("function ensureSharedLink", 1)[1].split(
        "].forEach(function (entry)", 1
    )[0]

    for path in ("/crush-map", "/telegram-alerts", "/users", "/clusters"):
        assert f'["{path}",' not in shared_block


def test_compact_admin_pages_keep_permission_gated_navigation_sources():
    templates = (
        "block_storage.html",
        "object_storage_buckets.html",
        "object_storage_bucket_detail.html",
        "object_storage_user_settings.html",
    )
    for name in templates:
        source = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
        for path in ("/crush-map", "/telegram-alerts", "/users", "/clusters"):
            assert f'href="{path}"' in source, f"{name} drops admin navigation {path}"
def test_object_storage_quota_link_uses_admin_navigation_capability():
    source = APP_JS.read_text(encoding="utf-8")
    assert 'linksByPath["/users"] || linksByPath["/clusters"]' in source
    assert 'if (!linksByPath["/object-storage/user-settings"] && objectStorageAdmin)' in source
