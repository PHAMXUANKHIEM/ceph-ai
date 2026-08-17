(function () {
  "use strict";

  var table = document.getElementById("block-storage-table");
  var input = document.getElementById("block-storage-search");
  var reset = document.getElementById("block-storage-filter-reset");
  var result = document.getElementById("block-storage-filter-result");
  var empty = document.getElementById("block-storage-filter-empty");
  var createForm = document.getElementById("block-storage-create-form");
  var createResult = document.getElementById("block-storage-create-result");
  if (!table || !input || !reset || !result || !empty) return;

  var rows = Array.prototype.slice.call(table.querySelectorAll(".block-storage-image-row"));
  function normalize(value) {
    return String(value || "").trim().toLocaleLowerCase("vi");
  }

  function applyFilter() {
    var query = normalize(input.value);
    var visible = 0;
    rows.forEach(function (row) {
      var haystack = normalize([
        row.dataset.name, row.dataset.pool, row.dataset.namespace
      ].join(" "));
      var matches = !query || haystack.indexOf(query) !== -1;
      row.hidden = !matches;
      if (matches) visible += 1;
    });
    result.textContent = visible + " / " + rows.length + " kết quả";
    reset.disabled = !query;
    empty.hidden = visible !== 0;
    table.hidden = visible === 0;
  }

  input.addEventListener("input", applyFilter);
  reset.addEventListener("click", function () {
    input.value = "";
    applyFilter();
    input.focus();
  });
  if (createForm) {
    createForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var button = createForm.querySelector('button[type="submit"]');
      var cluster = new URLSearchParams(window.location.search).get("cluster");
      var pool = createForm.elements.pool.value;
      var url = "/api/volumes/" + encodeURIComponent(pool) + "/inventory/create";
      if (cluster) url += "?cluster=" + encodeURIComponent(cluster);
      var key = window.crypto && window.crypto.randomUUID
        ? window.crypto.randomUUID()
        : "ui-" + Date.now() + "-" + Math.random().toString(16).slice(2);
      button.disabled = true;
      fetch(url, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "Idempotency-Key": key },
        body: JSON.stringify({
          image: createForm.elements.image.value.trim(),
          size_gib: Number(createForm.elements.size_gib.value)
        })
      }).then(function (response) {
        return response.json().catch(function () { return {}; }).then(function (body) {
          if (!response.ok) throw new Error(body.detail || "HTTP " + response.status);
          return body;
        });
      }).then(function (body) {
        createResult.hidden = false;
        createResult.className = "success";
        createResult.textContent = "Đã tạo đề xuất " + body.action_id + ". Hãy duyệt trong Audit Trail.";
        createForm.reset();
      }).catch(function (error) {
        createResult.hidden = false;
        createResult.className = "error";
        createResult.textContent = error.message;
      }).finally(function () { button.disabled = false; });
    });
  }
  applyFilter();
}());
