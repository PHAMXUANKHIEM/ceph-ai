(function () {
  "use strict";

  function splitTags(value) {
    return String(value || "")
      .split(",")
      .map(function (item) { return item.trim(); })
      .filter(Boolean);
  }

  function setupTagField(field) {
    var editor = field.querySelector(".cluster-tag-editor");
    var list = field.querySelector(".cluster-tag-list");
    var entry = field.querySelector(".cluster-tag-entry");
    var hidden = field.querySelector("input[type='hidden']");
    if (!editor || !list || !entry || !hidden) return;

    var tags = splitTags(hidden.value || editor.getAttribute("data-tag-value"));

    function sync() {
      hidden.value = tags.join(",");
      list.textContent = "";
      tags.forEach(function (tag, index) {
        var chip = document.createElement("span");
        chip.className = "cluster-node-tag";
        var text = document.createElement("code");
        text.textContent = tag;
        var remove = document.createElement("button");
        remove.type = "button";
        remove.className = "cluster-node-tag-remove";
        remove.setAttribute("aria-label", "Xóa " + tag);
        remove.textContent = "×";
        remove.addEventListener("click", function () {
          tags.splice(index, 1);
          sync();
          entry.focus();
        });
        chip.appendChild(text);
        chip.appendChild(remove);
        list.appendChild(chip);
      });
    }

    function addEntry() {
      var value = entry.value.trim().replace(/,$/, "");
      if (!value) return;
      value.split(",").map(function (item) { return item.trim(); }).filter(Boolean).forEach(function (item) {
        if (tags.indexOf(item) === -1) tags.push(item);
      });
      entry.value = "";
      sync();
    }

    entry.addEventListener("keydown", function (event) {
      if (event.key === "Enter" || event.key === ",") {
        event.preventDefault();
        addEntry();
      } else if (event.key === "Backspace" && !entry.value && tags.length) {
        tags.pop();
        sync();
      }
    });
    entry.addEventListener("blur", addEntry);
    editor.addEventListener("click", function () { entry.focus(); });
    sync();
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-tag-field]"), setupTagField);

  function updateContainerFields(select) {
    var form = select.form;
    if (!form) return;
    var compactMode = select.value === "cephadm" || select.value === "none";
    Array.prototype.forEach.call(form.querySelectorAll(".cluster-container-field"), function (field) {
      var input = field.querySelector("input");
      field.classList.toggle("is-not-needed", compactMode);
      if (input) input.disabled = compactMode;
      var help = field.querySelector(".cluster-field-help");
      if (help && compactMode) help.textContent = "Không cần khi dùng " + select.value + ".";
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-deploy-mode]"), function (select) {
    select.addEventListener("change", function () { updateContainerFields(select); });
    updateContainerFields(select);
  });

  Array.prototype.forEach.call(document.querySelectorAll("#cluster-create-form, .cluster-inline-edit-form"), function (form) {
    form.addEventListener("submit", function () {
      Array.prototype.forEach.call(form.querySelectorAll("[data-tag-field]"), function (field) {
        var entry = field.querySelector(".cluster-tag-entry");
        if (entry && entry.value.trim()) entry.dispatchEvent(new Event("blur"));
      });
    });
  });

  Array.prototype.forEach.call(document.querySelectorAll("[data-cluster-detail-target]"), function (button) {
    button.addEventListener("click", function () {
      var target = document.getElementById(button.getAttribute("data-cluster-detail-target"));
      if (!target) return;
      target.open = true;
      var menu = button.closest("details");
      if (menu) menu.open = false;
      target.scrollIntoView({ behavior: "smooth", block: "nearest" });
      window.setTimeout(function () {
        var summary = target.querySelector("summary");
        if (summary) summary.focus();
      }, 200);
    });
  });
})();
