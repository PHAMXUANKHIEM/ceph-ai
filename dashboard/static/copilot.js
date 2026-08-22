(function () {
  var form = document.getElementById("copilot-query-form");
  var input = document.getElementById("copilot-query");
  var result = document.getElementById("copilot-result");
  var submit = document.getElementById("copilot-submit");
  if (!form || !input || !result) return;
  document.querySelectorAll("[data-prompt]").forEach(function (button) {
    button.addEventListener("click", function () { input.value = button.dataset.prompt || ""; input.focus(); });
  });
  function renderReply(content) {
    result.replaceChildren();
    var marker = "\n\nNguồn đã kiểm chứng:\n";
    var markerAt = content.lastIndexOf(marker);
    var answer = document.createElement("div");
    answer.textContent = markerAt === -1 ? content : content.slice(0, markerAt);
    result.appendChild(answer);
    if (markerAt === -1) return;
    var sources = document.createElement("section");
    sources.className = "copilot-evidence-sources";
    var title = document.createElement("strong"); title.textContent = "✓ Nguồn đã kiểm chứng";
    var list = document.createElement("ul");
    content.slice(markerAt + marker.length).split("\n").filter(Boolean).forEach(function (line) {
      var item = document.createElement("li"); item.textContent = line.replace(/^[- ]+/, ""); list.appendChild(item);
    });
    sources.appendChild(title); sources.appendChild(list); result.appendChild(sources);
  }
  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var content = input.value.trim();
    if (!content) return;
    submit.disabled = true; result.textContent = "Đang đọc evidence và phân tích…";
    fetch("/api/chat/messages", {method:"POST", credentials:"same-origin", headers:{"Content-Type":"application/json"}, body:JSON.stringify({content:content})})
      .then(function (response) { if (!response.ok) throw new Error("HTTP " + response.status); return response.json(); })
      .then(function (data) { renderReply(data.assistant_message.content); })
      .catch(function (error) { result.textContent = "Không thể chạy Copilot: " + error.message; })
      .finally(function () { submit.disabled = false; });
  });
})();
