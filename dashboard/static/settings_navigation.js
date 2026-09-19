(function () {
  "use strict";

  // Keep Settings navigation available even when an optional provider/API
  // widget fails before settings.js reaches its navigation setup.
  if (window.__settingsNavigationReady) return;

  var items = Array.prototype.slice.call(document.querySelectorAll(".settings-nav-item[data-section]"));
  var panels = Array.prototype.slice.call(document.querySelectorAll(".settings-panel[data-panel]"));
  var groups = Array.prototype.slice.call(document.querySelectorAll(".settings-nav-group"));
  if (!items.length || !panels.length) return;

  function updateBreadcrumb(item) {
    var group = document.getElementById("settings-breadcrumb-group");
    var current = document.getElementById("settings-breadcrumb-current");
    if (!group || !current) return;
    var parent = item.closest(".settings-nav-group");
    var groupLabel = parent && parent.querySelector(".settings-nav-group-toggle span");
    group.textContent = groupLabel ? groupLabel.textContent.trim() : "Cài đặt";
    group.href = "#" + item.getAttribute("data-section");
    current.textContent = item.textContent.trim();
  }

  function activate(item) {
    var section = item.getAttribute("data-section");
    items.forEach(function (other) {
      other.classList.toggle("active", other === item);
    });
    panels.forEach(function (panel) {
      panel.hidden = panel.getAttribute("data-panel") !== section;
    });
    var parentItems = item.closest(".settings-nav-group-items");
    if (parentItems) {
      parentItems.hidden = false;
      var parentToggle = parentItems.parentElement.querySelector(".settings-nav-group-toggle");
      if (parentToggle) parentToggle.setAttribute("aria-expanded", "true");
    }
    updateBreadcrumb(item);
  }

  groups.forEach(function (group) {
    var toggle = group.querySelector(".settings-nav-group-toggle");
    var groupItems = group.querySelector(".settings-nav-group-items");
    if (!toggle || !groupItems) return;
    toggle.addEventListener("click", function () {
      groupItems.hidden = !groupItems.hidden;
      toggle.setAttribute("aria-expanded", groupItems.hidden ? "false" : "true");
    });
  });

  items.forEach(function (item) {
    item.addEventListener("click", function () { activate(item); });
  });

  var hash = window.location.hash.replace(/^#/, "");
  var initial = items.filter(function (item) {
    return item.getAttribute("data-section") === hash;
  })[0] || items.filter(function (item) {
    return item.classList.contains("active");
  })[0] || items[0];
  activate(initial);
})();
