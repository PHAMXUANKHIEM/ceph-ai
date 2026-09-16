(function () {
  "use strict";

  var table = document.getElementById("block-storage-table");
  var input = document.getElementById("block-storage-search");
  var reset = document.getElementById("block-storage-filter-reset");
  var empty = document.getElementById("block-storage-filter-empty");
  var createForm = document.getElementById("block-storage-create-form");
  var createResult = document.getElementById("block-storage-create-result");
  var mutationResult = document.getElementById("block-storage-mutation-result");
  if (!table) return;

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
    reset.disabled = !query;
    empty.hidden = visible !== 0;
    table.hidden = visible === 0;
  }

  if (input && reset && empty) {
    input.addEventListener("input", applyFilter);
    reset.addEventListener("click", function () {
      input.value = "";
      applyFilter();
      input.focus();
    });
  }
  function scopedUrl(path) {
    var url = new URL(path, window.location.origin);
    var cluster = new URLSearchParams(window.location.search).get("cluster");
    if (cluster) url.searchParams.set("cluster", cluster);
    return url.pathname + url.search;
  }

  function idempotencyKey() {
    return window.crypto && window.crypto.randomUUID
      ? window.crypto.randomUUID()
      : "ui-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  function proposeTrash(button) {
    var pool = button.dataset.pool;
    var image = button.dataset.image;
    if (!window.confirm("Đề xuất chuyển " + pool + "/" + image + " vào Trash? Thao tác cần được phê duyệt và sẽ bị chặn nếu Volume còn watcher, snapshot hoặc clone child.")) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    fetch(scopedUrl(
      "/api/volumes/" + encodeURIComponent(pool) + "/inventory/" + encodeURIComponent(image) + "/trash"
    ), {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey() },
      body: "{}"
    }).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (response.redirected && response.url.indexOf("/login") !== -1) {
          window.location.reload();
          throw new Error("unauthenticated");
        }
        if (!response.ok) throw new Error(body.detail || "HTTP " + response.status);
        if (!body.action_id) throw new Error("Server không trả về mã đề xuất");
        return body;
      });
    }).then(function (body) {
      if (mutationResult) {
        mutationResult.hidden = false;
        mutationResult.className = "success";
        mutationResult.textContent = "Đã tạo đề xuất " + body.action_id + ". Hãy duyệt trong Audit Trail.";
      }
      button.textContent = "Đã đề xuất";
    }).catch(function (error) {
      if (error.message === "unauthenticated") return;
      if (mutationResult) {
        mutationResult.hidden = false;
        mutationResult.className = "error";
        mutationResult.textContent = error.message;
      }
      button.disabled = false;
      button.removeAttribute("aria-busy");
    });
  }

  // Delegate the click from the table so the action keeps working after any
  // client-side table refresh/filter re-render, and always prevent a default
  // browser action from swallowing the request.
  table.addEventListener("click", function (event) {
    var button = event.target.closest
      ? event.target.closest(".block-storage-trash-btn")
      : null;
    if (!button || !table.contains(button) || button.disabled) return;
    event.preventDefault();
    proposeTrash(button);
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
  if (input && reset && result && empty) applyFilter();
}());
