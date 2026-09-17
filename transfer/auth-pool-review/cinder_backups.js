(function () {
  var dataNode = document.getElementById("cinder-backups-data");
  var panel = document.getElementById("cinder-backup-detail-panel");
  var backdrop = document.getElementById("cinder-backup-detail-backdrop");
  var closeButton = document.getElementById("cinder-backup-detail-close");
  var title = document.getElementById("cinder-backup-detail-title");
  var content = document.getElementById("cinder-backup-detail-content");
  if (!dataNode || !panel || !backdrop || !closeButton || !title || !content) return;

  var backups = [];
  var openTimer = null;
  try { backups = JSON.parse(dataNode.textContent || "[]"); } catch (error) { backups = []; }
  if (!Array.isArray(backups)) backups = [];

  function value(item, key) {
    return item && item[key] != null && String(item[key]).trim() ? String(item[key]) : "—";
  }
  function field(label, item, key) {
    var row = document.createElement("div"); row.className = "cinder-backup-detail-field";
    var labelNode = document.createElement("dt"); labelNode.textContent = label;
    var valueNode = document.createElement("dd"); valueNode.textContent = value(item, key);
    row.appendChild(labelNode); row.appendChild(valueNode); return row;
  }
  function show(item, message) {
    if (!item) return;
    title.textContent = value(item, "id"); content.replaceChildren();
    var identity = document.createElement("div"); identity.className = "cinder-backup-detail-identity";
    var name = document.createElement("strong"); name.textContent = value(item, "name"); identity.appendChild(name);
    var backend = document.createElement("span"); backend.className = "cinder-backup-backend cinder-backup-backend-" + value(item, "source"); backend.textContent = value(item, "source_label"); identity.appendChild(backend); content.appendChild(identity);
    if (message) { var note = document.createElement("p"); note.className = "cinder-backup-detail-note"; note.textContent = message; content.appendChild(note); }
    var list = document.createElement("dl"); list.className = "cinder-backup-detail-list";
    [["Backup ID", "id"], ["Volume ID", "volume_id"], ["Tên volume", "volume_name"], ["Volume type", "volume_type"], ["Kích thước", "size_gib"], ["Trạng thái", "status"], ["Thời gian", "created_at"], ["Container", "container"]].forEach(function (entry) { list.appendChild(field(entry[0], item, entry[1])); });
    content.appendChild(list); panel.hidden = false; backdrop.hidden = false; if (openTimer) window.clearTimeout(openTimer); openTimer = window.setTimeout(function () { panel.classList.add("is-open"); backdrop.classList.add("is-open"); }, 0); closeButton.focus();
  }
  function hide() {
    if (openTimer) { window.clearTimeout(openTimer); openTimer = null; }
    panel.classList.remove("is-open"); backdrop.classList.remove("is-open"); window.setTimeout(function () { panel.hidden = true; backdrop.hidden = true; }, 180);
  }
  Array.prototype.forEach.call(document.querySelectorAll(".cinder-backup-row"), function (row) {
    var index = Number(row.getAttribute("data-backup-index"));
    var item = backups[index];
    row.addEventListener("click", function () { show(item); });
    row.addEventListener("keydown", function (event) { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); show(item); } });
    Array.prototype.forEach.call(row.querySelectorAll("[data-action]"), function (button) {
      button.addEventListener("click", function (event) {
        event.stopPropagation();
        var action = button.getAttribute("data-action");
        if (action === "detail") show(item);
        if (action === "restore") show(item, "Restore Cinder backup chưa có endpoint trên dashboard; thao tác chưa được thực hiện.");
        if (action === "delete") {
          var form = row.querySelector(".cinder-backup-delete-form");
          if (form) { form.hidden = !form.hidden; if (!form.hidden) form.querySelector("input[name=confirmation]").focus(); }
        }
      });
    });
  });
  closeButton.addEventListener("click", hide); backdrop.addEventListener("click", hide);
  document.addEventListener("keydown", function (event) { if (event.key === "Escape" && !panel.hidden) hide(); });
})();
