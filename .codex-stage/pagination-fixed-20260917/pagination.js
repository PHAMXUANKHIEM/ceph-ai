(function (window) {
  "use strict";

  var PAGE_SIZE = 10;

  function pageItems(page, pageCount) {
    if (pageCount <= 7) return Array.from({ length: pageCount }, function (_item, index) { return index + 1; });
    if (page <= 4) return [1, 2, 3, 4, 5, "…", pageCount];
    if (page >= pageCount - 3) return [1, "…", pageCount - 4, pageCount - 3, pageCount - 2, pageCount - 1, pageCount];
    return [1, "…", page - 1, page, page + 1, "…", pageCount];
  }

  function renderPages(container, page, pageCount, onPageChange) {
    if (!container) return;
    container.replaceChildren();
    pageItems(page, pageCount).forEach(function (item) {
      if (item === "…") {
        var ellipsis = document.createElement("span");
        ellipsis.className = "pagination-ellipsis";
        ellipsis.textContent = "…";
        container.appendChild(ellipsis);
        return;
      }
      var button = document.createElement("button");
      button.type = "button";
      button.className = "pagination-page-number" + (item === page ? " is-active" : "");
      button.textContent = String(item);
      button.setAttribute("aria-label", "Trang " + item);
      if (item === page) {
        button.setAttribute("aria-current", "page");
        button.disabled = true;
      } else {
        button.addEventListener("click", function () { onPageChange(item); });
      }
      container.appendChild(button);
    });
  }

  function enhanceStaticPages() {
    document.querySelectorAll(".dashboard-pagination .pagination-pages").forEach(function (container) {
      var links = Array.prototype.slice.call(container.querySelectorAll("a"));
      if (links.length <= 7) return;
      var current = links.findIndex(function (link) { return link.getAttribute("aria-current") === "page"; });
      current = current < 0 ? 0 : current;
      var keep = new Set([0, links.length - 1, current, current - 1, current + 1]);
      links.forEach(function (link, index) { link.hidden = !keep.has(index); });
      var visible = links.filter(function (link) { return !link.hidden; });
      for (var index = 1; index < visible.length; index += 1) {
        var previous = links.indexOf(visible[index - 1]);
        var next = links.indexOf(visible[index]);
        if (next - previous > 1) {
          var ellipsis = document.createElement("span");
          ellipsis.className = "pagination-ellipsis";
          ellipsis.textContent = "…";
          visible[index].before(ellipsis);
        }
      }
    });
  }

  function pageFromText(value) {
    var match = String(value || "").match(/(\d+)\s*\/\s*(\d+)/);
    return match ? { current: Number(match[1]), total: Number(match[2]) } : { current: 1, total: 1 };
  }

  function findAnchor(pager) {
    var parent = pager.parentElement;
    while (parent && parent !== document.body) {
      var anchor = parent.querySelector(".table-wrap, .bucket-audit-table-frame, table, ul.crush-history-list");
      if (anchor && !anchor.contains(pager)) return anchor;
      parent = parent.parentElement;
    }
    return pager;
  }

  function proxyPageJump(input, original, page) {
    var target = Array.prototype.slice.call(original.querySelectorAll(".pagination-page-number"))
      .find(function (button) { return button.getAttribute("aria-label") === "Trang " + page; });
    if (target && !target.disabled) { target.click(); return; }
    input.setCustomValidity("Trang chưa khả dụng trong bộ phân trang hiện tại.");
    input.reportValidity();
  }

  function removePageSizeControls() {
    document.querySelectorAll(
      ".pagination-page-size, .block-storage-page-size, .pg-page-size"
    ).forEach(function (element) { element.remove(); });
  }

  function syncTop(top, original, anchor) {
    top.hidden = original.hidden;
    var summary = original.querySelector(".pagination-summary");
    var topSummary = top.querySelector(".pagination-summary");
    var status = original.querySelector(".pagination-mobile-status, .pagination-status");
    var state = pageFromText(status && status.textContent);
    if (summary && topSummary) {
      var compact = String(summary.textContent || "").replace(/^\s*Hiển thị\s+/i, "").replace(/\s+mục\s*$/i, "");
      topSummary.textContent = compact;
      topSummary.dataset.mobileSummary = compact;
    }
    var topPrev = top.querySelector(".pagination-prev");
    var topNext = top.querySelector(".pagination-next");
    var originalPrev = original.querySelector(".pagination-prev");
    var originalNext = original.querySelector(".pagination-next");
    [
      [topPrev, originalPrev, "Trang trước (←)"],
      [topNext, originalNext, "Trang sau (→)"],
    ].forEach(function (pair) {
      if (!pair[0] || !pair[1]) return;
      pair[0].disabled = Boolean(pair[1].disabled);
      pair[0].title = pair[2];
      pair[1].title = pair[2];
    });
    var jump = top.querySelector(".pagination-page-jump");
    if (jump) {
      jump.value = String(state.current);
      jump.max = String(Math.max(1, state.total));
      jump.setAttribute("aria-label", "Nhảy tới trang, tối đa " + state.total);
    }
    var rows = anchor.querySelectorAll ? anchor.querySelectorAll("tbody tr:not(.empty-row), li") : [];
    var itemMatch = summary && String(summary.textContent || "").match(/\/\s*(\d+)\s*(?:mục|items?|users?|volumes?|backups?|sự kiện|options?)/i);
    var itemTotal = itemMatch ? Number(itemMatch[1]) : rows.length;
    original.classList.toggle("pagination-bottom-single", itemTotal <= 5);
  }

  function installTopPager(original) {
    if (original.dataset.paginationEnhanced === "true") return;
    original.dataset.paginationEnhanced = "true";
    var top = original.cloneNode(true);
    top.classList.add("pagination-top");
    top.removeAttribute("id");
    top.removeAttribute("hidden");
    top.querySelectorAll("[id]").forEach(function (element) { element.removeAttribute("id"); });
    top.querySelectorAll(".pagination-pages").forEach(function (element) { element.setAttribute("aria-hidden", "true"); });
    var controls = top.querySelector(".pagination-controls");
    var next = top.querySelector(".pagination-next");
    var jump = document.createElement("input");
    jump.type = "number";
    jump.min = "1";
    jump.inputMode = "numeric";
    jump.className = "pagination-page-jump";
    jump.addEventListener("keydown", function (event) {
      if (event.key === "Enter") { event.preventDefault(); var state = pageFromText((original.querySelector(".pagination-mobile-status, .pagination-status") || {}).textContent); proxyPageJump(jump, original, Math.max(1, Math.min(state.total, Number(jump.value) || state.current))); }
    });
    if (controls && next) controls.insertBefore(jump, next);
    var topPrev = top.querySelector(".pagination-prev");
    var topNext = top.querySelector(".pagination-next");
    if (topPrev) topPrev.addEventListener("click", function () { var target = original.querySelector(".pagination-prev"); if (target && !target.disabled) target.click(); });
    if (topNext) topNext.addEventListener("click", function () { var target = original.querySelector(".pagination-next"); if (target && !target.disabled) target.click(); });
    var anchor = findAnchor(original);
    if (anchor !== original) anchor.before(top); else original.before(top);
    var focusTarget = anchor.querySelector ? (anchor.querySelector("table, ul") || anchor) : anchor;
    if (focusTarget && !focusTarget.hasAttribute("tabindex")) focusTarget.tabIndex = 0;
    if (focusTarget) focusTarget.addEventListener("keydown", function (event) {
      if (event.target.matches("input, select, button, a, textarea")) return;
      var button = event.key === "ArrowLeft" ? original.querySelector(".pagination-prev") : event.key === "ArrowRight" ? original.querySelector(".pagination-next") : null;
      if (button && !button.disabled) { event.preventDefault(); button.click(); }
    });
    var observer = new MutationObserver(function () { syncTop(top, original, anchor); });
    observer.observe(original, { childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "hidden", "class"] });
    if (anchor !== original) observer.observe(anchor, { childList: true, subtree: true, attributes: true, attributeFilter: ["hidden", "class"] });
    syncTop(top, original, anchor);
  }

  function installTopPagers() {
    document.querySelectorAll(".dashboard-pagination:not(.pagination-top)").forEach(function (pager) {
      if (pager.dataset.paginationManual === "true") return;
      // S3 Users owns both pagers itself: its audit pager is client-side
      // filtered and its inventory pager is server-side paginated. A cloned
      // shared pager creates two competing summaries and can restore the
      // stale 0–0 / 0 value.
      if (pager.closest && pager.closest(".s3-users-page")) return;
      installTopPager(pager);
    });
  }

  window.DashboardPagination = { pageItems: pageItems, renderPages: renderPages, pageSize: PAGE_SIZE };
  removePageSizeControls();
  enhanceStaticPages();
  installTopPagers();
  if (window.MutationObserver) {
    var pageObserver = new MutationObserver(function () {
      removePageSizeControls();
      installTopPagers();
    });
    pageObserver.observe(document.body, { childList: true, subtree: true });
  }
}(window));
