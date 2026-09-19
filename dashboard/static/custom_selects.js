(function () {
  "use strict";

  var sequence = 0;
  var enhanced = new WeakMap();
  var reactRoots = ["#ceph-dashboard-root", "#pools-dashboard-root"];

  function isReactOwned(select) {
    return reactRoots.some(function (selector) { return select.closest(selector); });
  }

  function enhance(select) {
    if (!select || enhanced.has(select) || isReactOwned(select) || select.multiple || select.size > 1) return;
    var wrapper = document.createElement("div");
    wrapper.className = "custom-select";
    wrapper.dataset.customSelect = "true";
    var id = select.id || "custom-select-" + (++sequence);
    var trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "custom-select-trigger";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", id + "-listbox");
    trigger.setAttribute("aria-label", select.getAttribute("aria-label") || select.name || "Chọn một giá trị");
    var chevron = document.createElement("span");
    chevron.className = "custom-select-chevron";
    chevron.setAttribute("aria-hidden", "true");
    chevron.textContent = "⌄";
    var menu = document.createElement("div");
    menu.className = "custom-select-menu";
    menu.id = id + "-listbox";
    menu.setAttribute("role", "listbox");
    menu.hidden = true;
    var search = null;
    var optionsHost = document.createElement("div");
    optionsHost.className = "custom-select-options";
    menu.appendChild(optionsHost);

    select.parentNode.insertBefore(wrapper, select);
    wrapper.appendChild(trigger);
    wrapper.appendChild(menu);
    wrapper.appendChild(select);
    enhanced.set(select, {wrapper: wrapper, trigger: trigger, menu: menu, optionsHost: optionsHost});
    select.classList.add("custom-select-native");

    function optionEntries() {
      return Array.from(select.options).map(function (option, index) {
        return {option: option, index: index, text: option.textContent.trim(), value: option.value};
      });
    }
    function selectedOption() {
      return select.options[select.selectedIndex] || select.options[0] || null;
    }
    function close(focusTrigger) {
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      wrapper.classList.remove("is-open");
      wrapper.classList.remove("is-flipped");
      if (focusTrigger) trigger.focus();
    }
    function closeOthers() {
      document.querySelectorAll(".custom-select.is-open").forEach(function (other) {
        if (other !== wrapper) {
          var otherTrigger = other.querySelector(".custom-select-trigger");
          if (otherTrigger) otherTrigger.click();
        }
      });
    }
    function visibleOptions() {
      return Array.from(optionsHost.querySelectorAll("[role=option]:not([hidden]):not([aria-disabled='true'])"));
    }
    function focusOption(optionElement) {
      if (!optionElement) return;
      optionElement.focus();
      trigger.setAttribute("aria-activedescendant", optionElement.id);
    }
    function choose(index) {
      var option = select.options[index];
      if (!option || option.disabled) return;
      select.selectedIndex = index;
      select.dispatchEvent(new Event("change", {bubbles: true}));
      sync();
      close(true);
    }
    function buildOptions() {
      var entries = optionEntries();
      if (search && search.parentNode) search.parentNode.removeChild(search);
      search = null;
      optionsHost.replaceChildren();
      // Show search for the shorter Ceph codename list as well as long
      // version lists (for example, Nautilus through Squid).
      if (entries.length >= 6) {
        search = document.createElement("input");
        search.type = "search";
        search.className = "custom-select-search";
        search.placeholder = "Tìm kiếm…";
        search.setAttribute("aria-label", "Tìm trong danh sách");
        menu.insertBefore(search, optionsHost);
        search.addEventListener("input", filterOptions);
        search.addEventListener("keydown", function (event) {
          if (event.key === "ArrowDown") { event.preventDefault(); focusOption(visibleOptions()[0]); }
          if (event.key === "ArrowUp") { event.preventDefault(); focusOption(visibleOptions()[visibleOptions().length - 1]); }
          if (event.key === "Enter") { event.preventDefault(); var first = visibleOptions()[0]; if (first) choose(Number(first.dataset.index)); }
          if (event.key === "Escape") { event.preventDefault(); close(true); }
        });
      }
      entries.forEach(function (entry) {
        var item = document.createElement("div");
        item.id = id + "-option-" + entry.index;
        item.className = "custom-select-option";
        item.setAttribute("role", "option");
        item.setAttribute("tabindex", "-1");
        item.dataset.index = String(entry.index);
        item.textContent = entry.text || "(Trống)";
        item.setAttribute("aria-disabled", entry.option.disabled ? "true" : "false");
        item.addEventListener("click", function () { choose(entry.index); });
        item.addEventListener("keydown", function (event) {
          var visible = visibleOptions();
          var current = visible.indexOf(item);
          if (event.key === "ArrowDown") { event.preventDefault(); focusOption(visible[Math.min(visible.length - 1, current + 1)]); }
          else if (event.key === "ArrowUp") { event.preventDefault(); if (current <= 0 && search) search.focus(); else focusOption(visible[Math.max(0, current - 1)]); }
          else if (event.key === "Enter" || event.key === " ") { event.preventDefault(); choose(entry.index); }
          else if (event.key === "Escape") { event.preventDefault(); close(true); }
          else if (event.key === "Tab") close(false);
        });
        optionsHost.appendChild(item);
      });
      sync();
    }
    function filterOptions() {
      var query = (search ? search.value : "").trim().toLowerCase();
      optionsHost.querySelectorAll("[role=option]").forEach(function (item) {
        item.hidden = query && item.textContent.toLowerCase().indexOf(query) < 0;
      });
    }
    function positionMenu() {
      if (menu.hidden) return;
      var triggerRect = trigger.getBoundingClientRect();
      var viewportPadding = 8;
      var gap = 6;
      var maxMenuHeight = 360;
      var below = window.innerHeight - triggerRect.bottom - gap - viewportPadding;
      var above = triggerRect.top - gap - viewportPadding;
      var minimumUsefulHeight = Math.min(240, Math.max(0, window.innerHeight - (viewportPadding * 2)));
      var flip = below < minimumUsefulHeight && above > below;
      var available = Math.max(120, flip ? above : below);
      var height = Math.min(maxMenuHeight, available);
      menu.style.position = "fixed";
      menu.style.left = Math.max(viewportPadding, triggerRect.left) + "px";
      menu.style.width = Math.min(triggerRect.width, window.innerWidth - viewportPadding * 2) + "px";
      menu.style.maxHeight = height + "px";
      menu.style.top = flip
        ? Math.max(viewportPadding, triggerRect.top - gap - height) + "px"
        : Math.min(window.innerHeight - viewportPadding - height, triggerRect.bottom + gap) + "px";
      wrapper.classList.toggle("is-flipped", flip);
    }
    function sync() {
      var selected = selectedOption();
      trigger.firstChild ? trigger.firstChild.textContent = selected ? selected.textContent.trim() : "Chọn…" : trigger.textContent = selected ? selected.textContent.trim() : "Chọn…";
      trigger.appendChild(chevron);
      trigger.disabled = select.disabled;
      trigger.setAttribute("aria-disabled", select.disabled ? "true" : "false");
      optionsHost.querySelectorAll("[role=option]").forEach(function (item) {
        var isSelected = Number(item.dataset.index) === select.selectedIndex;
        item.setAttribute("aria-selected", isSelected ? "true" : "false");
      });
    }
    function open() {
      if (select.disabled) return;
      closeOthers();
      if (search) { search.value = ""; filterOptions(); }
      menu.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      wrapper.classList.add("is-open");
      positionMenu();
      var selected = optionsHost.querySelector("[data-index='" + select.selectedIndex + "']");
      window.setTimeout(function () { focusOption(selected || visibleOptions()[0]); }, 0);
    }
    trigger.addEventListener("click", function () { menu.hidden ? open() : close(false); });
    trigger.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown" || event.key === "ArrowUp" || event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
      else if (event.key === "Escape" && !menu.hidden) { event.preventDefault(); close(false); }
    });
    window.addEventListener("resize", positionMenu);
    window.addEventListener("scroll", positionMenu, true);
    select.addEventListener("change", sync);
    var observer = new MutationObserver(function () { buildOptions(); });
    observer.observe(select, {childList: true, subtree: true, attributes: true, attributeFilter: ["disabled", "selected"]});
    var form = select.form;
    if (form) form.addEventListener("reset", function () { window.setTimeout(sync, 0); });
    buildOptions();
  }

  function enhanceAll(root) {
    (root || document).querySelectorAll("select:not([data-native-select])").forEach(enhance);
  }
  document.addEventListener("click", function (event) {
    if (!event.target.closest(".custom-select")) document.querySelectorAll(".custom-select.is-open").forEach(function (wrapper) {
      var trigger = wrapper.querySelector(".custom-select-trigger");
      if (trigger) trigger.click();
    });
  });
  document.addEventListener("DOMContentLoaded", function () {
    enhanceAll(document);
    var observer = new MutationObserver(function (records) {
      records.forEach(function (record) { record.addedNodes.forEach(function (node) { if (node.nodeType === 1) enhanceAll(node); }); });
    });
    observer.observe(document.body, {childList: true, subtree: true});
  });
})();
