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


def _main_nav_block() -> str:
    markup = (TEMPLATE_DIR / "_nav.html").read_text(encoding="utf-8")
    start = markup.index('<nav class="main-nav">')
    return markup[start:markup.index("</nav>", start)]


def test_every_dropdown_toggle_sits_inside_a_nav_dropdown_wrapper():
    """`app.js` gắn handler bằng `querySelectorAll('.nav-dropdown')` rồi tìm
    `.nav-dropdown-toggle` bên trong, còn CSS chỉ mở menu qua
    `.nav-dropdown:hover .nav-dropdown-menu`. Một toggle nằm ngoài wrapper là
    một menu không bao giờ mở được — nhóm Cluster từng mất wrapper và kéo
    theo 11 link (Pools, Volumes, Upgrade, Patch…) thành không tới được, mà
    cả bộ test điều hướng vẫn xanh vì href vẫn có trong HTML."""
    import re

    block = _main_nav_block()
    wrappers = re.findall(
        r'<div class="nav-dropdown">(.*?)</div>\s*</div>', block, re.S
    )
    toggles_in_wrappers = sum(chunk.count("nav-dropdown-toggle") for chunk in wrappers)

    assert block.count("nav-dropdown-toggle") == toggles_in_wrappers, (
        "có nav-dropdown-toggle nằm ngoài .nav-dropdown — menu đó sẽ không mở được"
    )


def test_main_navigation_div_tags_are_balanced():
    """Một `</div>` thừa sẽ đóng sớm `<nav>` và đẩy các mục còn lại ra ngoài."""
    import re

    block = _main_nav_block()
    assert len(re.findall(r"<div\b", block)) == len(re.findall(r"</div>", block))


def test_section_titles_are_siblings_not_nested_in_a_dropdown():
    import re

    block = _main_nav_block()
    for chunk in re.findall(r'<div class="nav-dropdown">(.*?)</div>\s*</div>', block, re.S):
        assert "sidebar-section-title" not in chunk, (
            "tiêu đề nhóm nằm trong .nav-dropdown sẽ render lọt vào bên trong dropdown"
        )


NUMERIC_TABLE_PAGES = (
    "volumes.html", "block_storage.html", "backups.html", "pgs.html",
    "object_storage_buckets.html", "object_storage_users.html",
)


def test_numeric_columns_declare_the_shared_alignment_class():
    """Số liệu canh trái với chữ số không đều bề ngang thì mắt không so được
    theo cột. `th.num`/`td.num` là một quy ước dùng chung, định nghĩa đúng
    một chỗ trong style.css."""
    for name in NUMERIC_TABLE_PAGES:
        markup = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
        assert 'class="num"' in markup, f"{name} chưa dùng quy ước canh số"


def test_shared_table_classes_are_defined_once():
    css = Path("dashboard/static/style.css").read_text(encoding="utf-8")
    assert css.count("th.num, td.num {") == 1
    assert css.count("td.truncate {") == 1


CHAT_WIDGET = Path("dashboard/static/chat_widget.js")


def test_assistant_markdown_is_built_as_dom_not_html_strings():
    """Nội dung tin nhắn đến từ mô hình ngôn ngữ — dữ liệu không tin cậy.
    Bộ render markdown phải dựng node bằng createElement/textContent; một
    lần `innerHTML = <chuỗi mô hình trả về>` là một lỗ XSS."""
    source = CHAT_WIDGET.read_text(encoding="utf-8")
    start = source.index("function appendInline(parent, text)")
    end = source.index("function buildAssistantContent(content)")
    renderer = source[start:end]

    assert "innerHTML" not in renderer
    assert "insertAdjacentHTML" not in renderer
    assert "createElement" in renderer and "textContent" in renderer


def test_chat_panel_no_longer_offsets_for_the_removed_horizontal_topbar():
    """`.topbar` đã thành sidebar dọc ở mục 1; panel vẫn trừ 53px thì hở một
    khoảng trên đỉnh và tràn 53px dưới đáy."""
    css = Path("dashboard/static/style.css").read_text(encoding="utf-8")
    assert "calc(100vh - 53px)" not in css
    assert "top: 53px" not in css
