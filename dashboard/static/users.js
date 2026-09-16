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
      var level = value.length === 0 ? "empty" : score <= 2 ? "weak" : score <= 3 ? "medium" : "strong";
      var labels = { empty: "Độ mạnh mật khẩu", weak: "Yếu", medium: "Trung bình", strong: "Mạnh" };
      strengthFill.className = level;
      strengthFill.style.width = { empty: "0%", weak: "33%", medium: "66%", strong: "100%" }[level];
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
})();
