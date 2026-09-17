(function (window) {
  "use strict";

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

  function fireChange(element) {
    var event;
    try { event = new Event("change", { bubbles: true }); }
    catch (_error) { event = document.createEvent("Event"); event.initEvent("change", true, true); }
    element.dispatchEvent(event);
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

  function makeSizeMenu(top, original) {
    var topSize = top.querySelector(".pagination-page-size");
    var originalSelect = original.querySelector(".pagination-page-size select");
    if (!topSize || !originalSelect || topSize.querySelector(".pagination-custom-size")) return;
    var label = document.createElement("span");
    label.textContent = "Mỗi trang";
    var custom = document.createElement("div");
    custom.className = "pagination-custom-size";
    var toggle = document.createElement("button");
    toggle.type = "button";
    toggle.className = "pagination-size-toggle";
    toggle.setAttribute("aria-haspopup", "listbox");
    toggle.setAttribute("aria-expanded", "false");
    var menu = document.createElement("div");
    menu.className = "pagination-size-menu";
    menu.setAttribute("role", "listbox");
    // Menus must be closed until the operator explicitly opens them.  Without
    // this, a newly-created menu is visible because the `hidden` attribute is
    // false by default, which makes every long-table pager look broken.
    menu.hidden = true;
    Array.prototype.slice.call(originalSelect.options).forEach(function (option) {
      var item = document.createElement("button");
      item.type = "button";
      item.className = "pagination-size-option";
      item.textContent = option.textContent;
      item.dataset.value = option.value;
      item.setAttribute("role", "option");
      item.addEventListener("click", function (event) {
        event.stopPropagation();
        originalSelect.value = option.value;
        fireChange(originalSelect);
        menu.hidden = true;
        toggle.setAttribute("aria-expanded", "false");
      });
      menu.appendChild(item);
    });
    toggle.addEventListener("click", function (event) {
      event.stopPropagation();
      menu.hidden = !menu.hidden;
      toggle.setAttribute("aria-expanded", String(!menu.hidden));
    });
    document.addEventListener("click", function () { menu.hidden = true; toggle.setAttribute("aria-expanded", "false"); });
    custom.append(toggle, menu);
    if (top === original) {
      originalSelect.classList.add("pagination-native-select-proxy");
      topSize.appendChild(custom);
    } else {
      topSize.replaceChildren(label, custom);
    }
    syncSizeMenu(top, original);
  }

  function syncSizeMenu(top, original) {
    var originalSelect = original.querySelector(".pagination-page-size select");
    var toggle = top.querySelector(".pagination-size-toggle");
    if (originalSelect && toggle) toggle.textContent = originalSelect.options[originalSelect.selectedIndex].textContent;
  }

  function proxyPageJump(input, original, page) {
    var target = Array.prototype.slice.call(original.querySelectorAll(".pagination-page-number"))
      .find(function (button) { return button.getAttribute("aria-label") === "Trang " + page; });
    if (target && !target.disabled) { target.click(); return; }
    input.setCustomValidity("Trang chưa khả dụng trong bộ phân trang hiện tại.");
    input.reportValidity();
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
    syncSizeMenu(top, original);
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
    makeSizeMenu(top, original);
    makeSizeMenu(original, original);
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
    document.querySelectorAll(".dashboard-pagination:not(.pagination-top)").forEach(installTopPager);
  }

  window.DashboardPagination = { pageItems: pageItems, renderPages: renderPages };
  enhanceStaticPages();
  installTopPagers();
  if (window.MutationObserver) {
    var pageObserver = new MutationObserver(installTopPagers);
    pageObserver.observe(document.body, { childList: true, subtree: true });
  }
}(window));
