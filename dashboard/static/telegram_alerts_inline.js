(function () {
  "use strict";

  var TOGGLE_ERROR = "Không thể thay đổi trạng thái kênh. Thử lại.";
  var toast = document.getElementById("telegram-toggle-toast");
  var toastTimer;
  var draggedCard = null;
  var pendingDeleteKey = null;
  var dialogMode = "create";

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () { toast.hidden = true; }, 4200);
  }

  function dialogOpen(dialog) {
    if (!dialog) return;
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "open");
  }

  function dialogClose(dialog) {
    if (!dialog) return;
    if (typeof dialog.close === "function") dialog.close();
    else dialog.removeAttribute("open");
  }

  function cardLabel(card) {
    return card && (card.dataset.channelLabel || (card.querySelector("[data-channel-name]") || {}).textContent || "kênh");
  }

  function updateCardIndexes() {
    var cards = document.querySelectorAll("#telegram-channel-grid .telegram-overview-item");
    Array.prototype.forEach.call(cards, function (card, index) {
      var indexEl = card.querySelector(".telegram-channel-index");
      if (indexEl) indexEl.textContent = String(index + 1).padStart(2, "0");
    });
    var count = document.getElementById("telegram-channel-count");
    if (count) count.textContent = cards.length + " kênh";
    var empty = document.getElementById("telegram-channel-empty");
    if (empty) empty.hidden = cards.length !== 0;
  }

  function setCardState(card, enabled, loading) {
    var button = card.querySelector(".telegram-switch");
    var badge = card.querySelector(".telegram-status-badge");
    var status = card.querySelector("[data-channel-status]");
    if (!button || !badge) return;
    var configured = card.classList.contains("is-unconfigured") === false || card.dataset.channelCustom === "true";
    card.dataset.channelEnabled = String(enabled);
    card.classList.toggle("is-on", enabled);
    card.classList.toggle("is-off", !enabled);
    if (configured) card.classList.remove("is-unconfigured");
    button.classList.toggle("is-on", enabled);
    button.classList.toggle("is-off", !enabled);
    button.classList.toggle("is-loading", loading);
    button.disabled = loading;
    button.setAttribute("aria-checked", String(enabled));
    button.setAttribute("aria-label", (enabled ? "Tắt " : "Bật ") + cardLabel(card));
    badge.classList.toggle("is-on", enabled);
    badge.classList.toggle("is-off", !enabled);
    if (status) status.textContent = enabled ? "Đang bật" : "Đã tắt";
  }

  function toggleCard(card, url, body, button, hidden) {
    var previous = card.dataset.channelEnabled === "true";
    var next = body.enabled === true;
    setCardState(card, next, true);
    fetch(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Accept": "application/json" },
      body: JSON.stringify(body)
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (data) {
        if (!response.ok) throw new Error(data.detail || TOGGLE_ERROR);
        return data;
      });
    }).then(function (data) {
      var finalEnabled = typeof data.enabled === "boolean" ? data.enabled : next;
      if (hidden) hidden.value = finalEnabled ? "false" : "true";
      setCardState(card, finalEnabled, false);
    }).catch(function () {
      if (hidden) hidden.value = previous ? "false" : "true";
      setCardState(card, previous, false);
      if (button) button.disabled = false;
      showToast(TOGGLE_ERROR);
    });
  }

  function toggleFixedForm(form) {
    var button = form.querySelector(".telegram-switch");
    var card = form.closest(".telegram-overview-item");
    var hidden = form.querySelector('input[name="enabled"]');
    if (!button || !card || !hidden || button.disabled) return;
    var next = hidden.value === "true";
    toggleCard(card, form.action, { enabled: next }, button, hidden);
  }

  document.querySelectorAll(".telegram-quick-toggle").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      toggleFixedForm(form);
    });
  });

  document.querySelectorAll("[data-channel-toggle]").forEach(function (button) {
    button.addEventListener("click", function () {
      var card = button.closest(".telegram-overview-item");
      if (!card || button.disabled) return;
      toggleCard(card, "/telegram-alerts/api/channels/" + encodeURIComponent(card.dataset.channelKey) + "/toggle", { enabled: card.dataset.channelEnabled !== "true" }, button, null);
    });
  });

  function formElements() {
    return {
      dialog: document.getElementById("telegram-channel-dialog"),
      form: document.getElementById("telegram-channel-form"),
      title: document.getElementById("telegram-channel-dialog-title"),
      description: document.getElementById("telegram-channel-dialog-description"),
      source: document.getElementById("telegram-channel-source-id"),
      key: document.getElementById("telegram-channel-edit-key"),
      name: document.getElementById("telegram-managed-name"),
      token: document.getElementById("telegram-managed-token"),
      tokenField: document.getElementById("telegram-managed-token-field"),
      chat: document.getElementById("telegram-managed-chat"),
      enabled: document.getElementById("telegram-managed-enabled"),
      error: document.getElementById("telegram-channel-dialog-error"),
      save: document.getElementById("telegram-channel-save")
    };
  }

  function openChannelModal(mode, card) {
    var el = formElements();
    dialogMode = mode;
    el.form.reset();
    el.source.value = "";
    el.key.value = "";
    el.error.hidden = true;
    el.tokenField.hidden = false;
    el.token.disabled = false;
    el.token.required = true;
    el.chat.required = true;
    el.enabled.closest("label").hidden = false;
    if (mode === "create") {
      el.title.textContent = "Thêm kênh Telegram";
      el.description.textContent = "Tạo một kênh mới. Kênh mới mặc định ở trạng thái tắt.";
      el.save.textContent = "Tạo kênh";
    } else if (mode === "duplicate") {
      el.title.textContent = "Nhân bản kênh";
      el.description.textContent = "Cấu hình Bot Token được sao chép an toàn ở server; hãy đổi tên hoặc Chat ID nếu cần.";
      el.save.textContent = "Tạo bản sao";
      el.source.value = card.dataset.channelKey;
      el.name.value = cardLabel(card) + " (bản sao)";
      el.chat.value = card.dataset.channelChatId || "";
      el.token.value = "";
      el.token.disabled = true;
      el.token.required = false;
      el.token.placeholder = "Giữ token từ kênh gốc";
    } else if (mode === "rename") {
      el.title.textContent = "Đổi tên kênh";
      el.description.textContent = "Tên hiển thị thay đổi ngay trên tổng quan; cấu hình gửi tin không đổi.";
      el.save.textContent = "Lưu tên";
      el.key.value = card.dataset.channelKey;
      el.name.value = cardLabel(card);
      el.tokenField.hidden = true;
      el.token.required = false;
      el.chat.required = false;
      el.enabled.closest("label").hidden = true;
    } else {
      el.title.textContent = "Cấu hình kênh";
      el.description.textContent = "Cập nhật tên, Chat ID hoặc trạng thái của kênh custom.";
      el.save.textContent = "Lưu cấu hình";
      el.key.value = card.dataset.channelKey;
      el.name.value = cardLabel(card);
      el.chat.value = card.dataset.channelChatId || "";
      el.tokenField.hidden = true;
      el.token.required = false;
      el.chat.required = true;
    }
    dialogOpen(el.dialog);
    window.setTimeout(function () { el.name.focus(); }, 0);
  }

  function closeMenus() {
    document.querySelectorAll(".telegram-channel-menu[open]").forEach(function (menu) { menu.open = false; });
  }

  function startInlineRename(card) {
    closeMenus();
    var name = card.querySelector("[data-channel-name]");
    if (!name || name.querySelector("input")) return;
    var original = name.textContent.trim();
    var input = document.createElement("input");
    input.type = "text";
    input.value = original;
    input.maxLength = 128;
    input.className = "telegram-inline-name-input";
    name.textContent = "";
    name.appendChild(input);
    input.focus();
    input.select();
    var finished = false;
    function finish(save) {
      if (finished) return;
      finished = true;
      var value = input.value.trim();
      if (!save || !value || value === original) { name.textContent = original; return; }
      fetch("/telegram-alerts/api/channels/" + encodeURIComponent(card.dataset.channelKey), {
        method: "PATCH", credentials: "same-origin", headers: { "Content-Type": "application/json", "Accept": "application/json" }, body: JSON.stringify({ name: value })
      }).then(function (response) { return response.json().then(function (data) { if (!response.ok) throw new Error(data.detail || "Không đổi được tên kênh"); return data; }); })
        .then(function () { card.dataset.channelLabel = value; name.textContent = value; })
        .catch(function (error) { name.textContent = original; showToast(error.message || "Không đổi được tên kênh"); });
    }
    input.addEventListener("keydown", function (event) { if (event.key === "Enter") { event.preventDefault(); finish(true); } if (event.key === "Escape") finish(false); });
    input.addEventListener("blur", function () { finish(true); });
  }

  document.querySelectorAll("[data-channel-action]").forEach(function (button) {
    button.addEventListener("click", function () {
      var card = button.closest(".telegram-overview-item");
      var action = button.dataset.channelAction;
      if (!card) return;
      if (action === "rename") startInlineRename(card);
      if (action === "duplicate") { closeMenus(); openChannelModal("duplicate", card); }
      if (action === "delete") { closeMenus(); openDeleteDialog(card); }
    });
  });

  function openDeleteDialog(card) {
    pendingDeleteKey = card.dataset.channelKey;
    var dialog = document.getElementById("telegram-delete-dialog");
    var message = document.getElementById("telegram-delete-message");
    if (message) message.textContent = "Xóa kênh " + cardLabel(card) + "? Hành động này không thể hoàn tác.";
    dialogOpen(dialog);
  }

  var deleteConfirm = document.getElementById("telegram-delete-confirm");
  if (deleteConfirm) deleteConfirm.addEventListener("click", function () {
    var key = pendingDeleteKey;
    var card = document.querySelector('[data-channel-key="' + CSS.escape(key || "") + '"]');
    if (!key || !card) return;
    var dialog = document.getElementById("telegram-delete-dialog");
    card.classList.add("is-removing");
    dialogClose(dialog);
    window.setTimeout(function () { card.remove(); updateCardIndexes(); }, 160);
    fetch("/telegram-alerts/api/channels/" + encodeURIComponent(key), { method: "DELETE", credentials: "same-origin", headers: { "Accept": "application/json" } })
      .then(function (response) { return response.json().then(function (data) { if (!response.ok) throw new Error(data.detail || "Không xóa được kênh"); return data; }); })
      .catch(function (error) { showToast(error.message || "Không xóa được kênh; đang tải lại trạng thái."); window.location.reload(); });
  });

  var channelForm = document.getElementById("telegram-channel-form");
  if (channelForm) channelForm.addEventListener("submit", function (event) {
    event.preventDefault();
    var el = formElements();
    var payload = { name: el.name.value.trim(), chat_id: el.chat.value.trim(), enabled: el.enabled.checked };
    var url;
    var method;
    if (dialogMode === "create" || dialogMode === "duplicate") {
      method = "POST"; url = "/telegram-alerts/api/channels"; payload.source_id = el.source.value;
      if (dialogMode === "create") payload.bot_token = el.token.value.trim();
    } else {
      method = "PATCH"; url = "/telegram-alerts/api/channels/" + encodeURIComponent(el.key.value); payload.enabled = el.enabled.checked;
    }
    el.save.disabled = true;
    fetch(url, { method: method, credentials: "same-origin", headers: { "Content-Type": "application/json", "Accept": "application/json" }, body: JSON.stringify(payload) })
      .then(function (response) { return response.json().then(function (data) { if (!response.ok) throw new Error(data.detail || "Không lưu được kênh"); return data; }); })
      .then(function () { dialogClose(el.dialog); window.location.reload(); })
      .catch(function (error) { el.error.textContent = error.message || "Không lưu được kênh"; el.error.hidden = false; el.save.disabled = false; });
  });

  document.querySelectorAll("[data-open-channel-modal]").forEach(function (button) {
    button.addEventListener("click", function () { openChannelModal(button.dataset.openChannelModal, button.closest(".telegram-overview-item")); });
  });
  document.querySelectorAll("[data-dialog-close]").forEach(function (button) {
    button.addEventListener("click", function () { dialogClose(button.closest("dialog")); });
  });

  function selectedCards() { return Array.prototype.filter.call(document.querySelectorAll("[data-channel-select]:checked"), function (input) { return input.closest(".telegram-overview-item"); }).map(function (input) { return input.closest(".telegram-overview-item"); }); }
  function updateBulkBar() {
    var cards = selectedCards();
    var bar = document.getElementById("telegram-bulk-bar");
    var count = document.getElementById("telegram-selected-count");
    if (count) count.textContent = cards.length;
    if (bar) bar.hidden = cards.length === 0;
  }
  document.querySelectorAll("[data-channel-select]").forEach(function (input) { input.addEventListener("change", updateBulkBar); });
  document.querySelectorAll("[data-bulk-clear]").forEach(function (button) { button.addEventListener("click", function () { document.querySelectorAll("[data-channel-select]:checked").forEach(function (input) { input.checked = false; }); updateBulkBar(); }); });
  document.querySelectorAll("[data-bulk-action]").forEach(function (button) {
    button.addEventListener("click", function () {
      var cards = selectedCards();
      var action = button.dataset.bulkAction;
      if (!cards.length) return;
      if (action === "delete" && !window.confirm("Xóa " + cards.length + " kênh đã chọn? Hành động này không thể hoàn tác.")) return;
      fetch("/telegram-alerts/api/channels/bulk", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "Accept": "application/json" }, body: JSON.stringify({ action: action, keys: cards.map(function (card) { return card.dataset.channelKey; }) }) })
        .then(function (response) { return response.json().then(function (data) { if (!response.ok) throw new Error(data.detail || "Bulk action thất bại"); return data; }); })
        .then(function () { window.location.reload(); })
        .catch(function (error) { showToast(error.message || "Bulk action thất bại"); });
    });
  });

  var grid = document.getElementById("telegram-channel-grid");
  if (grid) {
    grid.addEventListener("dragstart", function (event) { var card = event.target.closest(".telegram-overview-item"); if (!card) return; draggedCard = card; card.classList.add("is-dragging"); event.dataTransfer.effectAllowed = "move"; });
    grid.addEventListener("dragend", function () { if (draggedCard) draggedCard.classList.remove("is-dragging"); draggedCard = null; updateCardIndexes(); });
    grid.addEventListener("dragover", function (event) { event.preventDefault(); var target = event.target.closest(".telegram-overview-item"); if (!draggedCard || !target || target === draggedCard) return; var rect = target.getBoundingClientRect(); grid.insertBefore(draggedCard, event.clientY < rect.top + rect.height / 2 ? target : target.nextSibling); });
    grid.addEventListener("drop", function (event) { event.preventDefault(); if (!draggedCard) return; var order = Array.prototype.map.call(grid.querySelectorAll(".telegram-overview-item"), function (card) { return card.dataset.channelKey; }); fetch("/telegram-alerts/api/channels/reorder", { method: "POST", credentials: "same-origin", headers: { "Content-Type": "application/json", "Accept": "application/json" }, body: JSON.stringify({ order: order }) }).catch(function () { showToast("Không lưu được thứ tự kênh; đang tải lại."); window.location.reload(); }); });
  }
})();
