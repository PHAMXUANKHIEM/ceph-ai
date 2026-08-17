(function () {
  "use strict";

  var table = document.getElementById("block-storage-table");
  var input = document.getElementById("block-storage-search");
  var reset = document.getElementById("block-storage-filter-reset");
  var result = document.getElementById("block-storage-filter-result");
  var empty = document.getElementById("block-storage-filter-empty");
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
  applyFilter();
}());
