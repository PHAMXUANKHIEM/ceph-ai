from pathlib import Path


TEMPLATE_DIR = Path("dashboard/templates")
APP_JS = Path("dashboard/static/app.js")

SHARED_NAV_PATHS = {
    "/",
    "/nodes",
    "/volume-performance",
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
        # Partials are rendered inside a page template and must not load a
        # second copy of app.js.  `_nav.html` deliberately owns only markup.
        if template.name.startswith("_"):
            continue
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

    assert '"/object-storage/user-settings"' not in source
    assert 'paths: ["/object-storage/buckets", "/object-storage/users", "/bucket-access-log"]' in source
    assert 'paths: ["/block-storage", "/volume-performance", "/trash"]' in source
    assert 'if (path === "/volumes") link.textContent = "Volumes"' not in source
    assert '{ label: "Monitoring & Metrics", paths: ["/", "/nodes", "/crush-map"] }' in source
    assert '{ label: "Pool", paths: ["/pools", "/pgs"] }' in source


def test_permission_gated_links_are_not_synthesized_by_shared_navigation():
    source = APP_JS.read_text(encoding="utf-8")
    shared_block = source.split("function ensureSharedLink", 1)[1].split(
        "].forEach(function (entry)", 1
    )[0]

    for path in ("/crush-map", "/telegram-alerts", "/users", "/clusters"):
        assert f'["{path}",' not in shared_block


def test_compact_admin_pages_include_the_shared_permission_aware_navigation():
    templates = (
        "block_storage.html",
        "object_storage_buckets.html",
        "object_storage_bucket_detail.html",
        "object_storage_user_settings.html",
    )
    for name in templates:
        source = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
        assert '{% include "_nav.html" %}' in source, f"{name} omits the shared navigation"
def test_object_storage_quota_editor_is_not_added_to_global_navigation():
    source = APP_JS.read_text(encoding="utf-8")
    assert 'linksByPath["/object-storage/user-settings"]' not in source
    assert 'Quota & Capabilities' not in source
