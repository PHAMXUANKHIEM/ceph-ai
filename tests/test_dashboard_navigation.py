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


LOG_INTEL = TEMPLATE_DIR / "log_intelligence.html"


def test_collection_failure_message_is_not_truncated():
    """Tiêu chí của mục 6: không được giấu bất kỳ lần thu thập lỗi hay kết
    quả một phần nào. Cắt error_message ở 120 ký tự có thể cắt mất đúng lý
    do một lần quét FAILED."""
    markup = LOG_INTEL.read_text(encoding="utf-8")
    assert "run.error_message or '')[:120]" not in markup
    assert "log-scroll" in markup


def test_collection_outcomes_are_summarised_above_the_table():
    """Badge nằm ở cột 4 của bảng 10 cột thì phải dò cả bảng mới biết có lần
    quét nào hỏng."""
    markup = LOG_INTEL.read_text(encoding="utf-8")
    assert "collection-outcome-strip" in markup
    for status in ("OK", "PARTIAL", "FAILED"):
        assert f"'{status}')" in markup or f'"{status}")' in markup


def test_ai_hypotheses_are_marked_apart_from_measured_evidence():
    """Kết luận chưa kiểm chứng của AI và mẫu log thật đo được từng là hai
    hàng giống hệt nhau trong cùng một bảng."""
    markup = LOG_INTEL.read_text(encoding="utf-8")
    assert 'class="ai-claim"' in markup
    assert 'class="ai-evidence"' in markup
    css = Path("dashboard/static/style.css").read_text(encoding="utf-8")
    assert ".ai-claim td {" in css and ".ai-evidence th {" in css


def test_every_nav_entry_carries_an_initial_for_the_narrow_rail():
    """Ở sidebar 64px, nhãn bị ẩn và chỉ còn chữ viết tắt do `::before` sinh
    ra. Thiếu `data-initial` thì mục đó thành ô trống — đúng lỗi mà hai lần
    vá trước đều dính: `::first-letter` không áp dụng cho flex container,
    còn cắt tràn thì ra ký tự lẻ vô nghĩa."""
    import re

    markup = _main_nav_block()
    entries = re.findall(r'<(?:a|button)\b[^>]*class="nav-link[^"]*"[^>]*>', markup)
    assert entries, "không tìm thấy mục điều hướng nào"
    missing = [e for e in entries if "data-initial=" not in e]
    assert not missing, f"{len(missing)} mục thiếu data-initial: {missing[:2]}"


def test_narrow_rail_uses_generated_content_not_first_letter():
    """`::first-letter` không bao giờ áp dụng cho `.nav-link` vì nó là flex
    container; `::before` thì tạo ra một flex item thật — đúng cách mà nút
    logout trong file này vẫn dùng được."""
    css = Path("dashboard/static/style.css").read_text(encoding="utf-8")
    assert "nav-link::first-letter" not in css
    assert "content:attr(data-initial)" in css


def test_dynamic_navigation_labels_have_a_real_hideable_element():
    source = APP_JS.read_text(encoding="utf-8")
    css = Path("dashboard/static/style.css").read_text(encoding="utf-8")

    assert 'className = "nav-link-label"' in source
    assert ".nav-link-label { display: none; }" in css or ".nav-link-label{display:none}" in css
    assert ".nav-section-items[hidden] { display: flex; }" in css or ".nav-section-items[hidden]{display:flex}" in css


def test_tablet_rail_is_static_and_mobile_restores_full_labels():
    css = Path("dashboard/static/style.css").read_text(encoding="utf-8")

    assert "@media (min-width: 561px) and (max-width: 900px)" in css
    assert "position: static;" in css
    assert "@media (max-width: 560px)" in css
    assert ".app-shell .nav-link-label { display: inline; }" in css


def test_collapsed_desktop_rail_keeps_a_real_icon_width_and_no_pseudo_fragments():
    css = Path("dashboard/static/style.css").read_text(encoding="utf-8")

    assert "body.app-shell.sidebar-collapsed .main-nav" in css
    assert "align-self: stretch !important" in css
    assert "width: 100% !important" in css
    assert "body.app-shell.sidebar-collapsed .nav-link::before { content: none; }" in css
    assert ".app-shell .nav-link::before { content: none; }" in css


def test_mobile_drawer_exposes_keyboard_and_focus_return_contract():
    source = APP_JS.read_text(encoding="utf-8")

    assert 'menuButton.setAttribute("aria-controls", mainNav.id)' in source
    assert 'event.key === "Escape"' in source
    assert 'menuButton.focus();' in source
    assert 'event.key !== "Tab"' in source
    assert 'event.shiftKey && document.activeElement === first' in source
    assert 'document.activeElement === last' in source


def test_block_storage_create_panel_does_not_repeat_action_policy_copy():
    markup = (TEMPLATE_DIR / "block_storage.html").read_text(encoding="utf-8")

    assert "Action RISKY" not in markup
    assert "cần phê duyệt trước khi thực thi" not in markup
    assert "Đề xuất tạo Volume" in markup


def test_dashboard_approval_copy_uses_the_short_vietnamese_label():
    markup = (TEMPLATE_DIR / "index.html").read_text(encoding="utf-8")

    assert "Chờ duyệt</h2>" in markup
    assert "Risky Action" not in markup
    assert "Tự động mở khi có yêu cầu duyệt mới" in markup


def test_block_storage_does_not_repeat_count_and_page_capacity_copy():
    markup = (TEMPLATE_DIR / "block_storage.html").read_text(encoding="utf-8")
    script = Path("dashboard/static/block_storage.js").read_text(encoding="utf-8")

    assert "tối đa" not in markup
    assert "pagination-status" not in markup
    assert "block-storage-filter-result" not in markup
    assert "kết quả" not in script
    assert "if (input && reset && empty)" in script


PAGINATION_PAGES = {
    "alerts.html": "pagination",
    "pgs.html": "pg-pagination",
    "settings.html": "action-policy-pagination",
    "crush_map.html": "crush-history-pagination",
    "volumes.html": "trash-pagination",
}


def test_every_pagination_bar_marks_its_status_element():
    """Số trang được căn giữa bằng `grid-column: 2`, không dựa vào thứ tự —
    nút Trước/Sau là có điều kiện, nên khi một nút vắng mặt thì
    :first-child/:last-child trỏ sang nhầm phần tử và số trang lệch khỏi tâm."""
    for name in PAGINATION_PAGES:
        markup = (TEMPLATE_DIR / name).read_text(encoding="utf-8")
        assert "pagination-status" in markup, f"{name} thiếu .pagination-status"


def test_pagination_status_rule_wins_the_first_last_child_tie():
    """`.pagination > .pagination-status` và `.pagination > :first-child` có
    cùng độ đặc hiệu (0,2,0), nên rule đứng sau mới thắng. Đảo thứ tự là số
    trang lại rơi về cột 1 ở trang cuối."""
    css = Path("dashboard/static/style.css").read_text(encoding="utf-8")
    first = css.index(".pagination > :first-child")
    last = css.index(".pagination > :last-child")
    status = css.index(".pagination > .pagination-status")
    assert status > first and status > last


def test_pagination_uses_three_column_grid():
    css = Path("dashboard/static/style.css").read_text(encoding="utf-8")
    assert "grid-template-columns: 1fr auto 1fr;" in css
    assert ".pagination-end { display: flex;" in css
