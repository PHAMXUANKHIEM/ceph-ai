(function () {
  // Version picker: chọn dòng release rồi chọn phiên bản — same mechanic
  // as dashboard/static/deploy_cluster.js's picker (kept as a separate,
  // near-duplicate file rather than a shared module: this page has no
  // build step / shared-JS-module convention, every *.js here is loaded
  // as a plain <script> per page). Wires up every ".version-picker-form"
  // on the page independently (this page can have two: the cephadm
  // propose form and the ceph-deploy/package-download propose form),
  // scoped by DOM ancestry rather than by id so a third form could be
  // added later with zero JS changes.
  var dataEl = document.getElementById("versions-by-codename-data");
  if (!dataEl) {
    return; // not on /upgrade
  }
  var versionsByCodename = JSON.parse(dataEl.textContent || "{}");

  Array.prototype.forEach.call(document.querySelectorAll(".version-picker-form"), function (form) {
    var codenameSelect = form.querySelector(".version-picker-codename");
    var versionSelect = form.querySelector(".version-picker-version");
    var versionInput = form.querySelector(".version-picker-input");
    if (!codenameSelect || !versionSelect || !versionInput) return;

    codenameSelect.addEventListener("change", function () {
      var versions = versionsByCodename[codenameSelect.value] || [];
      versionSelect.innerHTML = "";
      if (!codenameSelect.value || versions.length === 0) {
        versionSelect.disabled = true;
        var placeholder = document.createElement("option");
        placeholder.value = "";
        placeholder.textContent = "— Chọn dòng release trước —";
        versionSelect.appendChild(placeholder);
        return;
      }
      versionSelect.disabled = false;
      // Newest point release of the line first — that's almost always
      // what an operator upgrading actually wants.
      for (var i = versions.length - 1; i >= 0; i--) {
        var option = document.createElement("option");
        option.value = versions[i];
        option.textContent = versions[i];
        versionSelect.appendChild(option);
      }
      versionInput.value = versions[versions.length - 1];
    });

    versionSelect.addEventListener("change", function () {
      if (versionSelect.value) versionInput.value = versionSelect.value;
    });
  });
})();
