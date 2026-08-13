(function () {
  var navItems = Array.prototype.slice.call(document.querySelectorAll(".settings-nav-item[data-section]"));
  var panels = Array.prototype.slice.call(document.querySelectorAll(".settings-panel[data-panel]"));
  navItems.forEach(function (button) {
    button.addEventListener("click", function () {
      navItems.forEach(function (item) { item.classList.toggle("active", item === button); });
      panels.forEach(function (panel) { panel.hidden = panel.dataset.panel !== button.dataset.section; });
      history.replaceState(null, "", "/vitastor/settings?section=" + encodeURIComponent(button.dataset.section));
    });
  });
  var provider = document.getElementById("vita-provider");
  var baseUrl = document.getElementById("vita-base-url");
  var apiKey = document.getElementById("vita-api-key");
  var model = document.getElementById("vita-model");
  var result = document.getElementById("vita-result");
  function show(ok, text) { result.hidden = false; result.className = "ai-test-result " + (ok ? "ai-test-ok" : "ai-test-fail"); result.textContent = (ok ? "✅ " : "❌ ") + text; }
  if (provider) provider.addEventListener("change", function () { var url = provider.options[provider.selectedIndex].dataset.baseUrl; if (url) baseUrl.value = url; });
  var verify = document.getElementById("vita-verify");
  if (verify) verify.addEventListener("click", function () {
    var body = new URLSearchParams({api_key: apiKey.value, base_url: baseUrl.value});
    fetch("/vitastor/settings/ai/verify", {method:"POST", credentials:"same-origin", body:body}).then(function(r){return r.json().then(function(d){if(!r.ok) throw new Error(d.detail); return d;});}).then(function(d){show(d.valid,d.message); if(d.valid){model.innerHTML=""; d.models.forEach(function(id){var o=document.createElement("option");o.value=id;o.textContent=id;model.appendChild(o);});}}).catch(function(e){show(false,e.message);});
  });
  var disconnect = document.getElementById("vita-disconnect");
  if (disconnect) disconnect.addEventListener("click", function(){if(!confirm("Huỷ kết nối AI của Vitastor?"))return;fetch("/vitastor/settings/ai/disconnect",{method:"POST",credentials:"same-origin"}).then(function(){location.reload();});});

  function accountRequest(path, options) {
    return fetch("/vitastor/settings/" + path, Object.assign({credentials:"same-origin"}, options || {})).then(function(response){return response.json().then(function(data){if(!response.ok)throw new Error(data.detail||"HTTP "+response.status);return data;});});
  }
  var codexStatus=document.getElementById("vita-codex-status"),codexLogin=document.getElementById("vita-codex-login"),codexLogout=document.getElementById("vita-codex-logout"),codexInstall=document.getElementById("vita-codex-install"),codexFlow=document.getElementById("vita-codex-flow");
  function renderCodex(data){if(!codexStatus)return;if(!data.installed){codexStatus.textContent="⚠️ Chưa cài Codex CLI.";codexInstall.hidden=false;codexLogin.hidden=true;}else if(data.authenticated){codexInstall.hidden=true;codexStatus.textContent="✅ Đã đăng nhập "+(data.email||"tài khoản ChatGPT")+(data.enabled?" — đang dùng cho Vitastor":"");codexLogin.hidden=!!data.enabled;codexLogout.hidden=!data.enabled;}else{codexInstall.hidden=true;codexStatus.textContent=data.error?"❌ "+data.error:"Chưa đăng nhập Codex.";codexLogin.hidden=false;codexLogout.hidden=true;}}
  function refreshCodex(activate){if(!codexStatus)return;accountRequest("codex/status").then(function(data){renderCodex(data);if(activate&&data.authenticated&&!data.enabled)return accountRequest("codex/activate",{method:"POST"}).then(function(){data.enabled=true;renderCodex(data);refreshClaude(false);});}).catch(function(e){codexStatus.textContent="❌ "+e.message;});}
  refreshCodex(false);
  var codexInstallBtn=document.getElementById("vita-codex-install-btn");if(codexInstallBtn)codexInstallBtn.onclick=function(){codexStatus.textContent="Đang cài Codex CLI…";accountRequest("codex/install",{method:"POST"}).then(function(){refreshCodex(false);}).catch(function(e){codexStatus.textContent="❌ "+e.message;});};
  if(codexLogin)codexLogin.onclick=function(){accountRequest("codex/login/start",{method:"POST"}).then(function(data){document.getElementById("vita-codex-link").href=data.verification_url;document.getElementById("vita-codex-code").textContent=data.user_code;codexFlow.hidden=false;window.open(data.verification_url,"_blank","noopener");var timer=setInterval(function(){accountRequest("codex/status").then(function(status){if(status.authenticated){clearInterval(timer);refreshCodex(true);}});},2500);}).catch(function(e){codexStatus.textContent="❌ "+e.message;});};
  if(codexLogout)codexLogout.onclick=function(){accountRequest("codex/logout",{method:"POST"}).then(function(){refreshCodex(false);});};

  var claudeStatusEl=document.getElementById("vita-claude-status"),claudeLogin=document.getElementById("vita-claude-login"),claudeLogoutBtn=document.getElementById("vita-claude-logout"),claudeFlow=document.getElementById("vita-claude-flow"),claudeInstallBox=document.getElementById("vita-claude-install");
  function renderClaude(data){if(!claudeStatusEl)return;if(!data.installed){claudeStatusEl.textContent="⚠️ Chưa cài Claude Code.";claudeInstallBox.hidden=false;claudeLogin.hidden=true;}else if(data.authenticated){claudeInstallBox.hidden=true;claudeStatusEl.textContent="✅ Đã đăng nhập "+(data.email||data.auth_method||"Claude")+(data.enabled?" — đang dùng cho Vitastor":"");claudeLogin.hidden=!!data.enabled;claudeLogoutBtn.hidden=!data.enabled;}else{claudeInstallBox.hidden=true;claudeStatusEl.textContent=data.error?"❌ "+data.error:"Chưa đăng nhập Claude.";claudeLogin.hidden=false;claudeLogoutBtn.hidden=true;}}
  function refreshClaude(activate){if(!claudeStatusEl)return;accountRequest("claude/status").then(function(data){renderClaude(data);if(activate&&data.authenticated&&!data.enabled)return accountRequest("claude/activate",{method:"POST"}).then(function(){data.enabled=true;renderClaude(data);refreshCodex(false);});}).catch(function(e){claudeStatusEl.textContent="❌ "+e.message;});}
  refreshClaude(false);
  var claudeInstallBtn=document.getElementById("vita-claude-install-btn");if(claudeInstallBtn)claudeInstallBtn.onclick=function(){claudeStatusEl.textContent="Đang cài Claude Code…";accountRequest("claude/install",{method:"POST"}).then(function(){refreshClaude(false);}).catch(function(e){claudeStatusEl.textContent="❌ "+e.message;});};
  if(claudeLogin)claudeLogin.onclick=function(){accountRequest("claude/login/start",{method:"POST"}).then(function(data){document.getElementById("vita-claude-link").href=data.verification_url;claudeFlow.hidden=false;window.open(data.verification_url,"_blank","noopener");}).catch(function(e){claudeStatusEl.textContent="❌ "+e.message;});};
  var claudeComplete=document.getElementById("vita-claude-complete");if(claudeComplete)claudeComplete.onclick=function(){var body=new URLSearchParams({authentication_code:document.getElementById("vita-claude-code").value});accountRequest("claude/login/complete",{method:"POST",body:body}).then(function(){return refreshClaude(true);}).catch(function(e){claudeStatusEl.textContent="❌ "+e.message;});};
  if(claudeLogoutBtn)claudeLogoutBtn.onclick=function(){accountRequest("claude/logout",{method:"POST"}).then(function(){refreshClaude(false);});};

  var dbForm = document.getElementById("vita-database-form");
  if (dbForm) {
    Array.prototype.forEach.call(dbForm.querySelectorAll('input[name="db_input_mode"]'), function (radio) {
      radio.addEventListener("change", function () {
        Array.prototype.forEach.call(dbForm.querySelectorAll("[data-vita-db-mode]"), function (group) { group.hidden = group.dataset.vitaDbMode !== radio.value; });
      });
    });
    document.getElementById("vita-db-test").addEventListener("click", function () {
      var body = new URLSearchParams(new FormData(dbForm));
      var output = document.getElementById("vita-db-result");
      fetch("/vitastor/settings/database/test", {method:"POST", credentials:"same-origin", body:body}).then(function(r){return r.json();}).then(function(data){output.hidden=false;output.className="ai-test-result "+(data.valid?"ai-test-ok":"ai-test-fail");output.textContent=(data.valid?"✅ ":"❌ ")+data.message;}).catch(function(err){output.hidden=false;output.className="ai-test-result ai-test-fail";output.textContent="❌ "+err.message;});
    });
  }
}());
