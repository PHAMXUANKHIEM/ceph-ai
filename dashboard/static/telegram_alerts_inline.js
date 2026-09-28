(function () {
  "use strict";

  var TOGGLE_ERROR = "Không thể thay đổi trạng thái kênh. Thử lại.";
  var toast = document.getElementById("telegram-toggle-toast");
  var toastTimer;

  function showToast(message) {
    if (!toast) return;
    toast.textContent = message;
    toast.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () { toast.hidden = true; }, 4200);
  }

  function setCardState(card, enabled, loading) {
    var button = card.querySelector(".telegram-switch");
    var badge = card.querySelector(".telegram-status-badge");
    var statusDot = badge && badge.querySelector(".telegram-status-dot");
    if (!button || !badge) return;

    card.classList.toggle("is-on", enabled);
    card.classList.toggle("is-off", !enabled);
    button.classList.toggle("is-on", enabled);
    button.classList.toggle("is-off", !enabled);
    button.classList.toggle("is-loading", loading);
    button.disabled = loading;
    button.setAttribute("aria-checked", String(enabled));
    button.setAttribute("aria-label", (enabled ? "Tắt " : "Bật ") + (card.dataset.channelLabel || "kênh"));
    badge.classList.toggle("is-on", enabled);
    badge.classList.toggle("is-off", !enabled);
    badge.lastChild.textContent = enabled ? "Đang bật" : "Đã tắt";
    if (statusDot) statusDot.setAttribute("aria-label", enabled ? "Đang bật" : "Đã tắt");
  }

  function toggleChannel(form) {
    var button = form.querySelector(".telegram-switch");
    var card = form.closest(".telegram-overview-item");
    var hidden = form.querySelector('input[name="enabled"]');
    if (!button || !card || !hidden || button.disabled) return;

    var previousEnabled = button.getAttribute("aria-checked") === "true";
    var nextEnabled = hidden.value === "true";
    setCardState(card, nextEnabled, true);

    fetch(form.action, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8", "Accept": "application/json" },
      body: new URLSearchParams({ enabled: String(nextEnabled) }).toString()
    }).then(function (response) {
      if (!response.ok) {
        return response.json().catch(function () { return {}; }).then(function (data) {
          throw new Error(data.detail || TOGGLE_ERROR);
        });
      }
      return response.json().catch(function () { return { enabled: nextEnabled }; });
    }).then(function (data) {
      var finalEnabled = typeof data.enabled === "boolean" ? data.enabled : nextEnabled;
      hidden.value = finalEnabled ? "false" : "true";
      setCardState(card, finalEnabled, false);
    }).catch(function () {
      hidden.value = previousEnabled ? "false" : "true";
      setCardState(card, previousEnabled, false);
      showToast(TOGGLE_ERROR);
    });
  }

  document.querySelectorAll(".telegram-quick-toggle").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      toggleChannel(form);
    });
  });

  document.querySelectorAll(".telegram-switch.is-disabled").forEach(function (button) {
    button.addEventListener("click", function () {
      if (button.dataset.configureUrl) window.location.href = button.dataset.configureUrl;
    });
  });
})();
