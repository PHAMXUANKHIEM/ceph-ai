(function () {
  "use strict";

  var table = document.getElementById("block-storage-table");
  var input = document.getElementById("block-storage-search");
  var poolFilter = document.getElementById("block-storage-pool-filter");
  var useFilter = document.getElementById("block-storage-use-filter");
  var reset = document.getElementById("block-storage-filter-reset");
  var empty = document.getElementById("block-storage-filter-empty");
  var createForm = document.getElementById("block-storage-create-form");
  var createResult = document.getElementById("block-storage-create-result");
  var mutationResult = document.getElementById("block-storage-mutation-result");
  var modal = document.getElementById("block-storage-create-modal");
  var openCreate = document.getElementById("block-storage-create-open");
  var closeCreate = document.getElementById("block-storage-create-close");
  var cancelCreate = document.getElementById("block-storage-create-cancel");
  if (!table) return;

  var rows = Array.prototype.slice.call(table.querySelectorAll(".block-storage-image-row"));
  function normalize(value) { return String(value || "").trim().toLocaleLowerCase("vi"); }

  function applyFilter() {
    var query = normalize(input.value);
    var selectedPool = normalize(poolFilter.value);
    var selectedUse = useFilter.value;
    var visible = 0;
    rows.forEach(function (row) {
      var usage = row.dataset.usedPercent === "" ? null : Number(row.dataset.usedPercent);
      var haystack = normalize([row.dataset.name, row.dataset.pool, row.dataset.namespace].join(" "));
      var matchesText = !query || haystack.indexOf(query) !== -1;
      var matchesPool = !selectedPool || normalize(row.dataset.pool) === selectedPool;
      var matchesUse = !selectedUse || (selectedUse === "used" ? usage !== null && usage > 0 : usage !== null && usage === 0);
      var matches = matchesText && matchesPool && matchesUse;
      row.hidden = !matches;
      if (matches) visible += 1;
    });
    reset.disabled = !query && !selectedPool && !selectedUse;
    empty.hidden = visible !== 0;
    table.hidden = visible === 0;
  }

  if (input && reset && empty) {
    if (poolFilter && useFilter) {
      input.addEventListener("input", applyFilter);
      poolFilter.addEventListener("change", applyFilter);
      useFilter.addEventListener("change", applyFilter);
      reset.addEventListener("click", function () {
        input.value = ""; poolFilter.value = ""; useFilter.value = ""; applyFilter(); input.focus();
      });
      if (rows.length) applyFilter();
    }
  }

  function scopedUrl(path) {
    var url = new URL(path, window.location.origin);
    var cluster = new URLSearchParams(window.location.search).get("cluster");
    if (cluster) url.searchParams.set("cluster", cluster);
    return url.pathname + url.search;
  }

  function idempotencyKey() {
    return window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : "ui-" + Date.now() + "-" + Math.random().toString(16).slice(2);
  }

  function apiJson(url, options) {
    var config = options || {};
    config.credentials = "same-origin";
    return fetch(url, config).then(function (response) {
      return response.json().catch(function () { return {}; }).then(function (body) {
        if (response.redirected && response.url.indexOf("/login") !== -1) { window.location.reload(); throw new Error("unauthenticated"); }
        if (!response.ok) throw new Error(body.detail || "HTTP " + response.status);
        return body;
      });
    });
  }

  function proposeTrash(button) {
    var pool = button.dataset.pool, image = button.dataset.image;
    if (!window.confirm("Đề xuất chuyển " + pool + "/" + image + " vào Trash? Thao tác cần được phê duyệt và sẽ bị chặn nếu Volume còn watcher, snapshot hoặc clone child.")) return;
    button.disabled = true; button.setAttribute("aria-busy", "true");
    apiJson(scopedUrl("/api/volumes/" + encodeURIComponent(pool) + "/inventory/" + encodeURIComponent(image) + "/trash"), {
      method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey() }, body: "{}"
    }).then(function (body) {
      if (mutationResult) { mutationResult.hidden = false; mutationResult.className = "success"; mutationResult.textContent = "Đã tạo đề xuất " + body.action_id + ". Hãy duyệt trong Audit Trail."; }
      button.textContent = "Đã đề xuất";
    }).catch(function (error) {
      if (error.message === "unauthenticated") return;
      if (mutationResult) { mutationResult.hidden = false; mutationResult.className = "error"; mutationResult.textContent = error.message; }
      button.disabled = false; button.removeAttribute("aria-busy");
    });
  }

  table.addEventListener("click", function (event) {
    var button = event.target.closest ? event.target.closest(".block-storage-trash-btn") : null;
    if (!button || !table.contains(button) || button.disabled) return;
    event.preventDefault(); event.stopPropagation(); proposeTrash(button);
  });

  function setModal(open) {
    if (!modal) return;
    modal.hidden = !open;
    document.body.classList.toggle("modal-open", open);
    if (open && createForm) { var first = createForm.elements.pool; if (first) first.focus(); }
    if (!open && openCreate) openCreate.focus();
  }
  if (openCreate) openCreate.addEventListener("click", function () { setModal(true); });
  if (closeCreate) closeCreate.addEventListener("click", function () { setModal(false); });
  if (cancelCreate) cancelCreate.addEventListener("click", function () { setModal(false); });
  if (modal) modal.addEventListener("click", function (event) { if (event.target === modal) setModal(false); });
  document.addEventListener("keydown", function (event) { if (event.key === "Escape" && modal && !modal.hidden) setModal(false); });


  if (createForm) {
    createForm.addEventListener("submit", function (event) {
      event.preventDefault();
      var button = createForm.querySelector('button[type="submit"]');
      var pool = createForm.elements.pool.value;
      var url = "/api/volumes/" + encodeURIComponent(pool) + "/inventory/create";
      var cluster = new URLSearchParams(window.location.search).get("cluster");
      if (cluster) url += "?cluster=" + encodeURIComponent(cluster);
      button.disabled = true;
      apiJson(url, { method: "POST", headers: { "Content-Type": "application/json", "Idempotency-Key": idempotencyKey() }, body: JSON.stringify({ image: createForm.elements.image.value.trim(), size_gib: Number(createForm.elements.size_gib.value) }) })
        .then(function (body) { createResult.hidden = false; createResult.className = "success"; createResult.textContent = "Đã tạo đề xuất " + body.action_id + ". Hãy duyệt trong Audit Trail."; createForm.reset(); })
        .catch(function (error) { if (error.message !== "unauthenticated") { createResult.hidden = false; createResult.className = "error"; createResult.textContent = error.message; } })
        .finally(function () { button.disabled = false; });
    });
  }
}());
