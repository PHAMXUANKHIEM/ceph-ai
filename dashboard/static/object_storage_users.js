(function () {
  var page = document.querySelector('.s3-users-page');
  if (!page) return;
  var cluster = page.getAttribute('data-cluster');
  var isAdmin = page.getAttribute('data-is-admin') === 'true';

  var userSearchForm = document.getElementById('s3-user-search-form');
  var userSearchInput = document.querySelector('[data-s3-user-search]');
  var userSearchStatus = document.querySelector('[data-s3-search-status]');
  var userSearchClear = document.querySelector('[data-s3-user-search-clear]');
  var userSearchTimer = null;
  var inventoryRefreshAttempts = 0;
  function updateUserSearchStatus() {
    if (!userSearchStatus || !userSearchInput) return;
    var value = userSearchInput.value.trim();
    userSearchStatus.hidden = !value;
    var code = userSearchStatus.querySelector('code');
    if (code) code.textContent = value;
  }
  function submitUserSearch() {
    if (!userSearchForm || !userSearchInput) return;
    var url = new URL(userSearchForm.action, window.location.origin);
    url.searchParams.set('cluster', cluster);
    url.searchParams.set('page', '1');
    var value = userSearchInput.value.trim();
    if (value) url.searchParams.set('query', value);
    window.location.assign(url.toString());
  }
  if (userSearchInput) {
    userSearchInput.addEventListener('input', function () {
      updateUserSearchStatus();
      window.clearTimeout(userSearchTimer);
      userSearchTimer = window.setTimeout(submitUserSearch, 300);
    });
  }
  if (userSearchForm) userSearchForm.addEventListener('submit', function (event) { event.preventDefault(); window.clearTimeout(userSearchTimer); submitUserSearch(); });
  if (userSearchClear) userSearchClear.addEventListener('click', function () { if (userSearchInput) userSearchInput.value = ''; submitUserSearch(); });
  updateUserSearchStatus();

  // A cold inventory is collected in the server-side cache executor. Poll
  // only the lightweight JSON endpoint, then reload once the snapshot is
  // ready; never start another Ceph command from the browser.
  function pollInventoryRefresh() {
    if (page.getAttribute('data-inventory-refreshing') !== 'true' || inventoryRefreshAttempts >= 45) return;
    inventoryRefreshAttempts += 1;
    var current = new URL(window.location.href);
    var apiUrl = new URL('/api/object-storage/users', window.location.origin);
    apiUrl.searchParams.set('cluster', cluster);
    ['query', 'page', 'page_size'].forEach(function (name) {
      var value = current.searchParams.get(name);
      if (value) apiUrl.searchParams.set(name, value);
    });
    window.fetch(apiUrl.toString(), {headers: {'Accept': 'application/json'}}).then(function (response) {
      if (!response.ok) throw new Error('inventory refresh failed');
      return response.json();
    }).then(function (body) {
      if (body.refreshing || body.searching) window.setTimeout(pollInventoryRefresh, 1000);
      else {
        var currentQuery = current.searchParams.get('query') || '';
        var inputDirty = userSearchInput && document.activeElement === userSearchInput
          && userSearchInput.value.trim() !== currentQuery;
        var auditVisible = document.getElementById('s3-audit-panel')
          && !document.getElementById('s3-audit-panel').hidden;
        if (inputDirty || auditVisible) {
          page.setAttribute('data-inventory-refreshing', 'false');
          var notice = document.querySelector('[data-s3-inventory-sync]');
          if (notice) notice.textContent = 'Snapshot S3 user đã sẵn sàng. Bấm Enter hoặc chuyển sang tab Danh sách để xem dữ liệu mới.';
          return;
        }
        window.location.reload();
      }
    }).catch(function () { window.setTimeout(pollInventoryRefresh, 1500); });
  }
  pollInventoryRefresh();

  function escapeHtml(value) {
    return String(value == null ? '' : value).replace(/[&<>"']/g, function (char) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[char];
    });
  }
  function api(path, options) {
    var separator = path.indexOf('?') >= 0 ? '&' : '?';
    return fetch(path + separator + 'cluster=' + encodeURIComponent(cluster), options || {})
      .then(function (response) { return response.json().then(function (body) { if (!response.ok) throw new Error(body.detail || 'Yêu cầu thất bại'); return body; }); });
  }
  function copy(value) {
    if (navigator.clipboard && navigator.clipboard.writeText) return navigator.clipboard.writeText(value);
    var input = document.createElement('textarea'); input.value = value; document.body.appendChild(input); input.select(); document.execCommand('copy'); input.remove(); return Promise.resolve();
  }
  function post(path, body) { return api(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)}); }

  var tabs = document.querySelectorAll('[data-s3-tab]');
  Array.prototype.forEach.call(tabs, function (tab) { tab.addEventListener('click', function () {
    Array.prototype.forEach.call(tabs, function (item) { var active = item === tab; item.classList.toggle('is-active', active); item.setAttribute('aria-selected', String(active)); });
    Array.prototype.forEach.call(document.querySelectorAll('.s3-tab-panel'), function (panel) { panel.hidden = panel.id !== tab.getAttribute('data-s3-tab'); });
    if (window.history && window.history.replaceState) window.history.replaceState(null, '', tab.getAttribute('data-s3-tab') === 's3-audit-panel' ? '#audit' : '#users');
  }); });

  var drawer = document.getElementById('s3-user-drawer');
  var drawerContent = document.getElementById('s3-user-detail-content');
  var drawerTitle = document.getElementById('s3-drawer-title');
  var currentUid = '';
  function closeDrawer() { drawer.setAttribute('aria-hidden', 'true'); document.body.classList.remove('s3-drawer-open'); }
  function openDrawer(uid) {
    currentUid = uid; drawer.setAttribute('aria-hidden', 'false'); document.body.classList.add('s3-drawer-open'); drawerContent.innerHTML = '<div class="s3-loading">Đang tải metadata…</div>';
    api('/api/object-storage/users/' + encodeURIComponent(uid)).then(function (detail) { renderDetail(detail); }).catch(function (error) { drawerContent.innerHTML = '<p class="error">' + escapeHtml(error.message) + '</p>'; });
  }
  Array.prototype.forEach.call(document.querySelectorAll('[data-s3-close-drawer]'), function (item) { item.addEventListener('click', closeDrawer); });
  document.addEventListener('keydown', function (event) { if (event.key === 'Escape') closeDrawer(); });
  Array.prototype.forEach.call(document.querySelectorAll('[data-s3-user-open]'), function (link) { link.addEventListener('click', function (event) { event.preventDefault(); openDrawer(link.getAttribute('data-s3-user-open')); }); });

  function statusBadge(suspended) { return '<span class="s3-status"><i class="s3-status-dot ' + (suspended ? 'is-suspended' : 'is-active') + '"></i>' + (suspended ? 'Suspended' : 'Active') + '</span>'; }
  function renderDetail(detail) {
    drawerTitle.textContent = detail.uid || currentUid;
    var keys = detail.access_keys || [];
    var keyRows = keys.length ? keys.map(function (key) { return '<tr><td><code>' + escapeHtml(key.access_key_masked) + '</code></td><td>' + escapeHtml(key.created_at || '—') + '</td><td><span class="s3-key-state">' + escapeHtml(key.status || 'active') + '</span></td><td>' + (isAdmin ? '<button type="button" class="btn btn-ghost btn-sm" data-s3-rotate="' + escapeHtml(detail.uid) + '" data-key-mask="' + escapeHtml(key.access_key_masked) + '">Rotate</button> <button type="button" class="btn btn-ghost btn-sm" data-s3-revoke="' + escapeHtml(detail.uid) + '" data-key-mask="' + escapeHtml(key.access_key_masked) + '">Revoke</button>' : '') + '</td></tr>'; }).join('') : '<tr><td colspan="4" class="hint">Không có access key.</td></tr>';
    drawerContent.innerHTML = '<div class="s3-detail-toolbar">' + (isAdmin ? '<button type="button" class="btn btn-primary btn-sm" data-s3-edit="' + escapeHtml(detail.uid) + '">Edit user</button><button type="button" class="btn btn-ghost btn-sm" data-s3-toggle="' + escapeHtml(detail.uid) + '" data-suspended="' + String(detail.suspended) + '">' + (detail.suspended ? 'Enable' : 'Suspend') + '</button><button type="button" class="btn btn-danger btn-sm" data-s3-delete="' + escapeHtml(detail.uid) + '">Delete</button>' : '') + '</div><div class="s3-detail-meta"><div><span>UID</span><strong>' + escapeHtml(detail.uid) + '</strong></div><div><span>Display name</span><strong>' + escapeHtml(detail.display_name || 'Chưa đặt tên') + '</strong></div><div><span>Email</span><strong>' + escapeHtml(detail.email || 'Chưa cài đặt') + '</strong></div><div><span>Max buckets</span><strong>' + escapeHtml(detail.max_buckets == null ? '—' : detail.max_buckets) + '</strong></div><div><span>Status</span><strong>' + statusBadge(detail.suspended) + '</strong></div></div><section class="s3-detail-section"><div class="s3-section-title"><h3>Access Keys</h3><button type="button" class="btn btn-primary btn-sm" data-s3-rotate="' + escapeHtml(detail.uid) + '">Rotate key</button></div><p class="hint">Chỉ hiện key đã masked. Secret mới chỉ hiển thị một lần trong wizard.</p><div class="table-wrap"><table class="s3-detail-table"><thead><tr><th>Access key</th><th>Created</th><th>Status</th><th></th></tr></thead><tbody>' + keyRows + '</tbody></table></div></section><section class="s3-detail-section"><div class="s3-section-title"><h3>Buckets</h3><span class="hint" data-s3-bucket-count>Đang tải…</span></div><div data-s3-buckets><div class="s3-loading">Đang tải bucket của user…</div></div></section><section class="s3-detail-section s3-danger-zone"><h3>Danger zone</h3><p class="hint">Delete yêu cầu preview và nhập lại UID. Lệnh không purge bucket để tránh xoá dữ liệu ngoài ý muốn.</p></section>';
    api('/api/object-storage/users/' + encodeURIComponent(detail.uid) + '/buckets').then(function (body) { var host = document.querySelector('[data-s3-buckets]'); var count = document.querySelector('[data-s3-bucket-count]'); if (count) count.textContent = (body.buckets || []).length + ' bucket'; if (host) host.innerHTML = body.buckets && body.buckets.length ? '<ul class="s3-bucket-list">' + body.buckets.map(function (bucket) { return '<li><code>' + escapeHtml(bucket) + '</code></li>'; }).join('') + '</ul>' : '<p class="hint">User này chưa sở hữu bucket nào.</p>'; }).catch(function (error) { var host = document.querySelector('[data-s3-buckets]'); if (host) host.innerHTML = '<p class="hint">Không đọc được bucket: ' + escapeHtml(error.message) + '</p>'; });
  }
  drawerContent.addEventListener('click', function (event) {
    var button = event.target.closest ? event.target.closest('button') : null; if (!button) return;
    var uid = button.getAttribute('data-s3-edit') || button.getAttribute('data-s3-toggle') || button.getAttribute('data-s3-delete') || button.getAttribute('data-s3-rotate') || button.getAttribute('data-s3-revoke'); if (!uid) return;
    if (button.hasAttribute('data-s3-edit')) openManagement('modify', uid);
    else if (button.hasAttribute('data-s3-toggle')) openManagement(button.getAttribute('data-suspended') === 'true' ? 'enable' : 'suspend', uid);
    else if (button.hasAttribute('data-s3-delete')) openManagement('delete', uid);
    else startKeyWizard(uid, button.getAttribute('data-key-mask') || '', button.hasAttribute('data-s3-revoke') ? 3 : 1);
  });

  var modal = document.getElementById('s3-user-modal');
  var actionForm = document.getElementById('s3-user-action-form');
  var approved = null;
  function showModal() { if (modal.showModal) modal.showModal(); else modal.setAttribute('open', ''); }
  function hideModal() { if (modal.close) modal.close(); else modal.removeAttribute('open'); }
  function openManagement(action, uid) { if (!modal) return; document.getElementById('s3-modal-title').textContent = action === 'create' ? 'Tạo S3 user' : 'Cập nhật S3 user'; document.getElementById('s3-action').value = action; document.getElementById('s3-uid').value = uid || ''; document.getElementById('s3-display-name').value = ''; document.getElementById('s3-email').value = ''; document.getElementById('s3-preview').hidden = true; document.getElementById('s3-create-credential').hidden = true; document.getElementById('s3-action-status').textContent = ''; showModal(); }
  if (modal) {
    document.getElementById('s3-open-create').addEventListener('click', function () { openManagement('create', ''); });
    Array.prototype.forEach.call(document.querySelectorAll('[data-s3-close-modal]'), function (item) { item.addEventListener('click', hideModal); });
    document.getElementById('s3-action').addEventListener('change', function () { document.getElementById('s3-modal-title').textContent = this.value === 'create' ? 'Tạo S3 user' : 'Cập nhật S3 user'; });
    actionForm.addEventListener('submit', function (event) { event.preventDefault(); var payload = {action: document.getElementById('s3-action').value, uid: document.getElementById('s3-uid').value.trim(), display_name: document.getElementById('s3-display-name').value.trim(), email: document.getElementById('s3-email').value.trim()}; approved = null; document.getElementById('s3-action-status').textContent = 'Đang tạo preview…'; post('/api/object-storage/users/actions/preview', payload).then(function (data) { approved = payload; document.getElementById('s3-preview-summary').textContent = data.preview + ' · rủi ro ' + data.risk; document.getElementById('s3-preview-command').textContent = data.preview; document.getElementById('s3-confirmation').value = ''; document.getElementById('s3-preview').hidden = false; document.getElementById('s3-action-status').textContent = 'Nhập lại UID để bật nút thực thi.'; }).catch(function (error) { document.getElementById('s3-action-status').textContent = 'Lỗi: ' + error.message; }); });
    document.getElementById('s3-confirmation').addEventListener('input', function () { document.getElementById('s3-execute').disabled = !approved || this.value !== approved.uid; });
    document.getElementById('s3-execute').addEventListener('click', function () { if (!approved) return; var button = this; button.disabled = true; post('/api/object-storage/users/actions/execute', Object.assign({}, approved, {confirmation: document.getElementById('s3-confirmation').value})).then(function (data) { if (data.credential) { document.getElementById('s3-created-access-key').textContent = data.credential.access_key; document.getElementById('s3-created-secret-key').textContent = data.credential.secret_key; document.getElementById('s3-create-credential').hidden = false; } else { hideModal(); window.location.reload(); } }).catch(function (error) { document.getElementById('s3-action-status').textContent = 'Lỗi: ' + error.message; button.disabled = false; }); });
  }

  if (isAdmin && document.getElementById('s3-action') && !document.querySelector('#s3-action option[value="delete"]')) { var deleteOption = document.createElement('option'); deleteOption.value = 'delete'; deleteOption.textContent = 'Xoá user'; document.getElementById('s3-action').appendChild(deleteOption); }
  var wizard = {uid: '', oldMask: '', step: 1};
  function startKeyWizard(uid, oldMask, step) { wizard.uid = uid; wizard.oldMask = oldMask; wizard.step = step || 1; renderWizard(); }
  function renderWizard() { var existing = document.getElementById('s3-key-wizard'); if (existing) existing.remove(); var box = document.createElement('section'); box.id = 's3-key-wizard'; box.className = 's3-wizard'; box.innerHTML = '<div class="s3-section-title"><h3>Access-key lifecycle</h3><button type="button" class="btn btn-ghost btn-sm" data-s3-wizard-close>Đóng</button></div><ol class="s3-wizard-steps"><li class="' + (wizard.step >= 1 ? 'is-current' : '') + '">1. Tạo key mới</li><li class="' + (wizard.step >= 2 ? 'is-current' : '') + '">2. Cập nhật client</li><li class="' + (wizard.step >= 3 ? 'is-current' : '') + '">3. Revoke key cũ</li></ol><div data-s3-wizard-body></div>'; drawerContent.prepend(box); var body = box.querySelector('[data-s3-wizard-body]'); if (wizard.step === 1) body.innerHTML = '<p>Key mới sẽ được tạo qua preview/execute. Secret chỉ hiện một lần.</p><button type="button" class="btn btn-primary" data-s3-create-key>Tạo key mới</button><p class="hint" data-s3-wizard-status></p>'; if (wizard.step === 2) body.innerHTML = '<p class="s3-warning-box">Lưu credential mới trước khi rời bước này. Secret chỉ hiện một lần.</p><p>Access key: <code>' + escapeHtml(wizard.newAccess || '') + '</code> <button type="button" class="btn btn-ghost btn-sm" data-copy-wizard="access">Copy</button></p><p>Secret key: <code>' + escapeHtml(wizard.newSecret || '') + '</code> <button type="button" class="btn btn-ghost btn-sm" data-copy-wizard="secret">Copy</button></p><label class="s3-check"><input type="checkbox" data-s3-client-confirm> Tôi đã cập nhật client và kiểm tra key mới hoạt động</label><button type="button" class="btn btn-primary" data-s3-to-revoke disabled>Tiếp tục revoke key cũ</button>'; if (wizard.step === 3) body.innerHTML = '<p>Key cũ đang chọn: <code>' + escapeHtml(wizard.oldMask || 'chưa chọn') + '</code></p><p class="hint">Vì key đầy đủ không được trả về browser, hãy nhập access key cũ để xác nhận revoke.</p><label>Access key cũ<input type="password" data-s3-old-key autocomplete="off"></label><button type="button" class="btn btn-danger" data-s3-revoke-key>Preview &amp; revoke</button><p class="hint" data-s3-wizard-status></p>'; }
  drawerContent.addEventListener('click', function (event) { var target = event.target; if (target.hasAttribute('data-s3-wizard-close')) { var box = document.getElementById('s3-key-wizard'); if (box) box.remove(); } if (target.hasAttribute('data-s3-create-key')) { target.disabled = true; post('/api/object-storage/users/keys/preview', {action: 'create_key', uid: wizard.uid}).then(function () { return post('/api/object-storage/users/keys/execute', {action: 'create_key', uid: wizard.uid, confirmation: wizard.uid}); }).then(function (data) { wizard.newAccess = data.credential.access_key; wizard.newSecret = data.credential.secret_key; wizard.step = 2; renderWizard(); }).catch(function (error) { target.disabled = false; var status = document.querySelector('[data-s3-wizard-status]'); if (status) status.textContent = 'Lỗi: ' + error.message; }); } if (target.hasAttribute('data-copy-wizard')) { var value = target.getAttribute('data-copy-wizard') === 'access' ? wizard.newAccess : wizard.newSecret; copy(value).then(function () { target.textContent = 'Đã copy'; }); } if (target.hasAttribute('data-s3-to-revoke')) { wizard.step = 3; renderWizard(); } if (target.hasAttribute('data-s3-revoke-key')) { var oldKey = document.querySelector('[data-s3-old-key]').value.trim(); var status = document.querySelector('[data-s3-wizard-status]'); if (!oldKey) { status.textContent = 'Cần nhập access key cũ.'; return; } target.disabled = true; post('/api/object-storage/users/keys/preview', {action: 'revoke_key', uid: wizard.uid, access_key: oldKey}).then(function () { return post('/api/object-storage/users/keys/execute', {action: 'revoke_key', uid: wizard.uid, access_key: oldKey, confirmation: oldKey}); }).then(function () { status.textContent = 'Đã revoke key cũ. Đang tải lại detail…'; openDrawer(wizard.uid); }).catch(function (error) { target.disabled = false; status.textContent = 'Lỗi: ' + error.message; }); } });
  drawerContent.addEventListener('change', function (event) { if (event.target.hasAttribute('data-s3-client-confirm')) { var next = document.querySelector('[data-s3-to-revoke]'); if (next) next.disabled = !event.target.checked; } });

  function classifyAction(value) { var action = value.toLowerCase(); if (/(delete|remove|revoke|purge|destroy|trash)/.test(action)) return 'is-delete'; if (/(create|modify|enable|suspend|set|add|update|rotate|disable)/.test(action)) return 'is-write'; return 'is-read'; }
  var auditRows = Array.prototype.slice.call(document.querySelectorAll('[data-s3-audit-row]')); var auditPage = 1; var auditSize = 10; var auditPageSizeLabel = '10 dòng/trang'; var auditSearch = document.querySelector('[data-s3-audit-search]'); var auditAction = document.querySelector('[data-s3-audit-action]'); var auditResult = document.querySelector('[data-s3-audit-result]');
  if (auditRows.length && auditAction) { auditRows.forEach(function (row) { var action = row.getAttribute('data-action'); var option = document.createElement('option'); option.value = action; option.textContent = action; auditAction.appendChild(option); var actionCell = row.querySelector('.s3-audit-action'); if (actionCell) actionCell.classList.add(classifyAction(action)); var result = row.getAttribute('data-result'); var badge = row.querySelector('[data-s3-result]'); if (badge) badge.classList.add(result === 'succeeded' ? 'is-success' : result === 'failed' ? 'is-failed' : 'is-pending'); var time = row.querySelector('.s3-audit-time'); if (time) { var date = new Date(time.getAttribute('datetime')); if (!isNaN(date.getTime())) time.textContent = date.toLocaleString('en-US', {month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false}); } }); var noResults = document.querySelector('[data-s3-audit-no-results]'); var status = document.querySelector('[data-s3-audit-status]'); var summary = document.querySelector('[data-s3-audit-summary]'); var pageButtons = document.querySelector('[data-s3-audit-pages]'); var previous = document.querySelector('[data-s3-audit-prev]'); var next = document.querySelector('[data-s3-audit-next]'); function renderAudit() { var query = (auditSearch.value || '').toLowerCase().trim(); var filtered = auditRows.filter(function (row) { return (!query || row.textContent.toLowerCase().indexOf(query) >= 0) && (!auditAction.value || row.getAttribute('data-action') === auditAction.value) && (!auditResult.value || row.getAttribute('data-result') === auditResult.value); }); var pages = Math.max(1, Math.ceil(filtered.length / auditSize)); auditPage = Math.min(auditPage, pages); auditRows.forEach(function (row) { row.hidden = true; }); filtered.slice((auditPage - 1) * auditSize, auditPage * auditSize).forEach(function (row) { row.hidden = false; }); if (noResults) noResults.hidden = filtered.length !== 0 || auditRows.length === 0; var first = filtered.length ? ((auditPage - 1) * auditSize + 1) : 0; var last = Math.min(auditPage * auditSize, filtered.length); if (summary) { summary.textContent = 'Hiển thị ' + first + '–' + last + ' / ' + filtered.length + ' mục'; summary.dataset.mobileSummary = first + '–' + last + ' / ' + filtered.length; } if (status) status.textContent = 'Trang ' + auditPage + '/' + pages + ' · ' + auditPageSizeLabel; if (window.DashboardPagination && pageButtons) window.DashboardPagination.renderPages(pageButtons, auditPage, pages, function (target) { auditPage = target; renderAudit(); }); if (previous) previous.disabled = auditPage === 1; if (next) next.disabled = auditPage === pages; } [auditSearch, auditAction, auditResult].forEach(function (control) { if (control) control.addEventListener(control === auditSearch ? 'input' : 'change', function () { auditPage = 1; renderAudit(); }); }); if (previous) previous.addEventListener('click', function () { auditPage = Math.max(1, auditPage - 1); renderAudit(); }); if (next) next.addEventListener('click', function () { auditPage += 1; renderAudit(); }); renderAudit(); }
  var purgeButton = document.querySelector('[data-s3-audit-purge]');
  var purgeStatus = document.querySelector('[data-s3-audit-purge-status]');
  var purgeModal = document.getElementById('s3-audit-purge-modal');
  var purgeCount = document.querySelector('[data-s3-audit-purge-count]');
  var purgeConfirm = document.querySelector('[data-s3-audit-purge-confirm]');
  var purgeCancelButtons = document.querySelectorAll('[data-s3-audit-purge-cancel]');
  function closePurgeModal() {
    if (purgeModal && purgeModal.close) purgeModal.close();
    else if (purgeModal) purgeModal.removeAttribute('open');
  }
  function executePurge() {
    if (!purgeButton) return;
    closePurgeModal();
    purgeButton.disabled = true;
    if (purgeStatus) { purgeStatus.hidden = false; purgeStatus.className = 's3-audit-purge-status'; purgeStatus.textContent = 'Đang xóa audit…'; }
    post('/api/object-storage/audit/purge', {confirmation: 'DELETE_ALL_AUDIT'})
      .then(function (body) {
        auditRows.forEach(function (row) { row.remove(); });
        auditRows = [];
        auditPage = 1;
        if (auditAction) auditAction.value = '';
        if (auditResult) auditResult.value = '';
        if (auditSearch) auditSearch.value = '';
        renderAudit();
        if (noResults) {
          noResults.hidden = false;
          var emptyMessage = noResults.querySelector('.empty-state');
          if (emptyMessage) emptyMessage.textContent = 'Chưa có bản ghi audit.';
        }
        if (purgeStatus) { purgeStatus.textContent = 'Đã xóa ' + body.deleted + ' bản ghi audit.'; purgeStatus.className = 's3-audit-purge-status is-success'; }
      })
      .catch(function (error) {
        if (purgeStatus) { purgeStatus.hidden = false; purgeStatus.className = 's3-audit-purge-status is-error'; purgeStatus.textContent = 'Không thể xóa audit: ' + error.message; }
      })
      .finally(function () { purgeButton.disabled = false; });
  }
  if (purgeButton && isAdmin) {
    purgeButton.addEventListener('click', function () {
      if (purgeCount) purgeCount.textContent = String(auditRows.length);
      if (purgeModal && purgeModal.showModal) purgeModal.showModal();
      else if (purgeModal) purgeModal.setAttribute('open', '');
      else if (window.confirm('Xóa toàn bộ audit của cluster đang chọn?')) executePurge();
    });
  }
  Array.prototype.forEach.call(purgeCancelButtons, function (button) { button.addEventListener('click', closePurgeModal); });
  if (purgeConfirm) purgeConfirm.addEventListener('click', executePurge);
  Array.prototype.forEach.call(document.querySelectorAll('[data-copy-value]'), function (button) { button.addEventListener('click', function () { copy(button.getAttribute('data-copy-value')).then(function () { button.classList.add('is-copied'); button.title = 'Đã copy'; }); }); });
  Array.prototype.forEach.call(document.querySelectorAll('[data-copy-target]'), function (button) { button.addEventListener('click', function () { var node = document.getElementById(button.getAttribute('data-copy-target')); copy(node.textContent).then(function () { button.textContent = 'Đã copy'; }); }); });
}());

// Keep the audit pager truthful even if a browser restores a stale DOM state
// or the main enhancement script runs before the server-rendered rows settle.
(function () {
  var rows = Array.prototype.slice.call(document.querySelectorAll('[data-s3-audit-row]'));
  var summaryNodes = Array.prototype.slice.call(document.querySelectorAll('[data-s3-audit-summary]'));
  if (!rows.length || !summaryNodes.length) return;
  var stale = summaryNodes.some(function (node) { return /^\s*(?:Hiển thị\s+)?0[–-]0\s*\/\s*0/.test(node.textContent || ''); });
  if (!stale) return;
  var size = 10;
  var last = Math.min(size, rows.length);
  summaryNodes.forEach(function (node) {
    node.textContent = 'Hiển thị 1–' + last + ' / ' + rows.length + ' mục';
    node.dataset.mobileSummary = '1–' + last + ' / ' + rows.length;
  });
  rows.forEach(function (row, index) { row.hidden = index >= size; });
  var status = document.querySelector('[data-s3-audit-status]');
  if (status) status.textContent = 'Trang 1/' + Math.max(1, Math.ceil(rows.length / size)) + ' · 10 dòng/trang';
  var previous = document.querySelector('[data-s3-audit-prev]');
  var next = document.querySelector('[data-s3-audit-next]');
  if (previous) previous.disabled = true;
  if (next) next.disabled = rows.length <= size;
}());
