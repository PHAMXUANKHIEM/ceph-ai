(function () {
  function byId(id) { return document.getElementById(id); }

  Array.prototype.forEach.call(document.querySelectorAll("[data-password-toggle]"), function (button) {
    button.addEventListener("click", function () {
      var input = byId(button.getAttribute("data-password-toggle"));
      if (!input) return;
      var showing = input.type === "text";
      input.type = showing ? "password" : "text";
      button.setAttribute("aria-label", showing ? "Hiện mật khẩu" : "Ẩn mật khẩu");
    });
  });

  var password = byId("new-password");
  var strengthFill = byId("password-strength-fill");
  var strengthLabel = byId("password-strength-label");
  if (password && strengthFill && strengthLabel) {
    password.addEventListener("input", function () {
      var value = password.value;
      var score = 0;
      if (value.length >= 8) score += 1;
      if (value.length >= 12) score += 1;
      if (/[a-z]/.test(value) && /[A-Z]/.test(value)) score += 1;
      if (/\d/.test(value)) score += 1;
      if (/[^A-Za-z0-9]/.test(value)) score += 1;
      var level = value.length === 0 ? "empty" : score <= 2 ? "weak" : score <= 3 ? "medium" : score === 4 ? "strong" : "very-strong";
      var labels = { empty: "Độ mạnh mật khẩu", weak: "Yếu", medium: "Trung bình", strong: "Mạnh", "very-strong": "Rất mạnh" };
      var segments = document.querySelectorAll(".password-strength-track .strength-segment");
      var activeCount = { empty: 0, weak: 1, medium: 2, strong: 3, "very-strong": 4 }[level];
      Array.prototype.forEach.call(segments, function (segment, index) {
        segment.className = "strength-segment " + (index < activeCount ? level : "");
      });
      strengthLabel.textContent = labels[level];
    });
  }

  Array.prototype.forEach.call(document.querySelectorAll(".user-role-option input"), function (input) {
    input.addEventListener("change", function () {
      Array.prototype.forEach.call(document.querySelectorAll(".user-role-option"), function (option) {
        option.classList.toggle("is-selected", !!option.querySelector("input:checked"));
      });
    });
  });

  function relativeTime(value) {
    if (!value) return "—";
    var normalized = /(?:Z|[+-]\d\d:\d\d)$/.test(value) ? value : value + "Z";
    var date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return value;
    var seconds = Math.max(0, Math.floor((Date.now() - date.getTime()) / 1000));
    if (seconds < 60) return "vừa xong";
    var minutes = Math.floor(seconds / 60);
    if (minutes < 60) return minutes + " phút trước";
    var hours = Math.floor(minutes / 60);
    if (hours < 24) return hours + " giờ trước";
    var days = Math.floor(hours / 24);
    if (days < 30) return days + " ngày trước";
    var months = Math.floor(days / 30);
    if (months < 12) return months + " tháng trước";
    return Math.floor(months / 12) + " năm trước";
  }
  Array.prototype.forEach.call(document.querySelectorAll(".user-relative-time"), function (time) {
    time.textContent = relativeTime(time.getAttribute("data-created-at"));
  });

  function closeToast(toast) {
    if (!toast || toast.classList.contains("is-dismissing")) return;
    toast.classList.add("is-dismissing");
    window.setTimeout(function () { toast.remove(); }, 220);
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-user-toast]"), function (toast) {
    var dismiss = toast.querySelector(".users-feedback-dismiss");
    if (dismiss) dismiss.addEventListener("click", function () { closeToast(toast); });
    window.setTimeout(function () { closeToast(toast); }, 5000);
  });

  var usersPage = document.querySelector(".users-page");
  var openCreate = byId("users-open-create");
  var closeCreate = byId("users-close-create");
  var backdrop = byId("users-drawer-backdrop");
  function setDrawer(open) {
    if (!usersPage) return;
    usersPage.classList.toggle("is-user-drawer-open", !!open);
    if (backdrop) backdrop.hidden = !open;
    document.body.classList.toggle("users-drawer-is-open", !!open);
    if (open && closeCreate) closeCreate.focus();
  }
  if (openCreate) openCreate.addEventListener("click", function () { setDrawer(true); });
  if (closeCreate) closeCreate.addEventListener("click", function () { setDrawer(false); });
  if (backdrop) backdrop.addEventListener("click", function () { setDrawer(false); });
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") setDrawer(false);
  });

  var activeModal = null;
  var pendingDangerForm = null;
  function showModal(modal) {
    if (!modal) return;
    activeModal = modal;
    modal.hidden = false;
    document.body.classList.add("users-modal-is-open");
    var focusTarget = modal.querySelector("input:not([type=hidden]), button:not([data-modal-close])");
    if (focusTarget) window.setTimeout(function () { focusTarget.focus(); }, 0);
  }
  function hideModal() {
    if (activeModal) activeModal.hidden = true;
    activeModal = null;
    pendingDangerForm = null;
    document.body.classList.remove("users-modal-is-open");
  }
  Array.prototype.forEach.call(document.querySelectorAll("[data-modal-close]"), function (button) {
    button.addEventListener("click", hideModal);
  });

  var editModal = byId("user-edit-modal");
  var editForm = byId("user-edit-form");
  var editUsername = byId("edit-username");
  var editChatAi = byId("edit-chat-ai");
  Array.prototype.forEach.call(document.querySelectorAll("[data-user-edit]"), function (button) {
    button.addEventListener("click", function () {
      if (!editModal || !editForm) return;
      var isAdmin = button.dataset.userAdmin === "true";
      editForm.action = "/users/" + encodeURIComponent(button.dataset.userId) + "/edit";
      if (editUsername) editUsername.value = button.dataset.userName || "";
      var role = editModal.querySelector('input[name="edit_is_admin"][value="' + (isAdmin ? "on" : "") + '"]');
      if (role) role.checked = true;
      if (editChatAi) { editChatAi.checked = button.dataset.userAi === "true"; editChatAi.disabled = isAdmin; }
      showModal(editModal);
    });
  });

  var passwordModal = byId("user-password-modal");
  var passwordForm = byId("user-password-form");
  var passwordUserName = byId("password-user-name");
  Array.prototype.forEach.call(document.querySelectorAll("[data-user-password]"), function (button) {
    button.addEventListener("click", function () {
      if (!passwordModal || !passwordForm) return;
      passwordForm.action = "/users/" + encodeURIComponent(button.dataset.userId) + "/change-password";
      if (passwordUserName) passwordUserName.textContent = button.dataset.userName || "user";
      Array.prototype.forEach.call(passwordForm.querySelectorAll("input[type=password]"), function (input) { input.value = ""; });
      showModal(passwordModal);
    });
  });

  var dangerModal = byId("user-danger-modal");
  var dangerMessage = byId("user-danger-message");
  var dangerTitle = byId("user-danger-title");
  var dangerField = byId("user-danger-confirm-field");
  var dangerInput = byId("user-danger-confirm-input");
  var dangerConfirm = byId("user-danger-confirm");
  function syncDangerButton() {
    if (!dangerConfirm) return;
    dangerConfirm.disabled = !!(dangerField && !dangerField.hidden && (!dangerInput || dangerInput.value !== dangerInput.dataset.expected));
  }
  if (dangerInput) dangerInput.addEventListener("input", syncDangerButton);
  Array.prototype.forEach.call(document.querySelectorAll("form[data-danger-action]"), function (form) {
    if (!form.dataset.dangerAction) return;
    form.addEventListener("submit", function (event) {
      if (!dangerModal) return;
      event.preventDefault();
      pendingDangerForm = form;
      var username = form.dataset.dangerUsername || "user";
      var deleting = form.dataset.dangerAction === "delete";
      if (dangerTitle) dangerTitle.textContent = deleting ? "Xóa tài khoản" : "Vô hiệu hóa tài khoản";
      if (dangerMessage) dangerMessage.textContent = deleting
        ? "Bạn có chắc muốn xóa vĩnh viễn user " + username + "? Hành động này không thể hoàn tác."
        : "Bạn có chắc muốn vô hiệu hóa user " + username + "? User này sẽ không thể đăng nhập cho đến khi được kích hoạt lại.";
      if (dangerField) dangerField.hidden = !deleting;
      if (dangerInput) { dangerInput.value = ""; dangerInput.dataset.expected = username; }
      if (dangerConfirm) {
        dangerConfirm.textContent = deleting ? "Xóa vĩnh viễn" : "Vô hiệu hóa";
        dangerConfirm.className = "btn " + (deleting ? "is-danger" : "is-warning");
      }
      syncDangerButton();
      showModal(dangerModal);
    });
  });
  if (dangerConfirm) {
    dangerConfirm.addEventListener("click", function () {
      if (!pendingDangerForm || dangerConfirm.disabled) return;
      pendingDangerForm.submit();
    });
  }
  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && activeModal) hideModal();
  });
})();
