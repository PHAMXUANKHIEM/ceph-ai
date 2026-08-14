(function () {
  var panelEl = document.getElementById("chat-panel");
  var bodyEl = document.getElementById("chat-panel-body");
  var historyBtn = document.getElementById("chat-panel-history");
  var settingsBtn = document.getElementById("chat-panel-settings");
  var newSessionBtn = document.getElementById("chat-panel-new-session");
  var minimizeBtn = document.getElementById("chat-panel-minimize");
  var closeBtn = document.getElementById("chat-panel-close");
  var messagesEl = document.getElementById("chat-messages");
  var formEl = document.getElementById("chat-form");
  var historyListViewEl = document.getElementById("chat-history-list-view");
  var historyListEl = document.getElementById("chat-history-list");
  var settingsViewEl = document.getElementById("chat-settings-view");
  var settingsFormEl = document.getElementById("chat-settings-form");
  var aiNameInputEl = document.getElementById("chat-ai-name");
  var settingsSuccessEl = document.getElementById("chat-settings-success");
  var panelAiNameEl = document.getElementById("chat-panel-ai-name");
  if (!panelEl || !bodyEl || !messagesEl || !formEl) {
    return; // not on a page with the chat panel
  }

  // The id of the conversation new messages get tagged with — learned from
  // GET /api/chat/messages on load (whatever the most recent message's
  // session was) and replaced by POST /api/chat/sessions's response when
  // the operator starts a new one. null before either has run; the backend
  // treats a missing/blank session_id on send as "start a new session too",
  // so this is never required to be non-null before sending.
  var currentSessionId = null;
  var aiName = "AI";
  var apiPrefix = panelEl.getAttribute("data-api-prefix") || "/api/chat";
  var settingsUrl = panelEl.getAttribute("data-settings-url") || "/settings";
  var productName = panelEl.getAttribute("data-product-name") || "Ceph";

  var inputEl = document.getElementById("chat-input");
  var sendBtn = document.getElementById("chat-send-btn");
  var errorEl = document.getElementById("chat-error");

  var MINIMIZED_STORAGE_KEY = "chatPanelMinimized";
  var MINIMIZE_ICON = "−"; // −
  var RESTORE_ICON = "□"; // □
  var TYPING_ID = "chat-typing-indicator";
  // Must match dashboard/chat_client.py's MISSING_AI_CONFIG_MESSAGE exactly
  // — the backend sends this as plain text (chat bubbles never carry HTML),
  // so the frontend detects this exact known sentinel to render an actual
  // clickable Settings link instead of just the raw text.
  var MISSING_AI_CONFIG_MESSAGE = "⚙️ Chưa kết nối AI. Vào Settings để kết nối API, Codex hoặc Claude.";
  var NETWORK_ERROR_MESSAGE = "Không thể kết nối server. Thử lại sau.";

  // Always renders in Asia/Ho_Chi_Minh regardless of the viewing browser's
  // own OS timezone — deliberately NOT getHours()/getMinutes() etc. (those
  // read the browser-local interpretation, which isn't guaranteed to be
  // Vietnam time even for a Vietnam-based operator). The backend always
  // sends a real UTC instant (dashboard/vntime.py::to_utc_iso, explicit
  // "Z" suffix) — before that fix, a timezone-less ISO string was parsed
  // by `new Date()` as already being browser-local per the ECMAScript
  // spec, which silently showed raw UTC digits mislabeled as local time.
  function _vnParts(d) {
    var parts = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Ho_Chi_Minh",
      year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", second: "2-digit",
      hour12: false,
    }).formatToParts(d);
    var map = {};
    parts.forEach(function (p) { map[p.type] = p.value; });
    return map;
  }
  function formatTimestamp(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    var p = _vnParts(d);
    return p.hour + ":" + p.minute + ":" + p.second;
  }
  function formatDateTime(iso) {
    var d = new Date(iso);
    if (isNaN(d.getTime())) return iso;
    var p = _vnParts(d);
    return p.year + "-" + p.month + "-" + p.day + " " + p.hour + ":" + p.minute;
  }

  function handleAuthRedirect(response) {
    if (response.redirected && response.url.indexOf("/login") !== -1) {
      window.location.reload();
      throw new Error("unauthenticated");
    }
    return response;
  }

  function clearEmptyState() {
    var empty = document.getElementById("chat-empty-state");
    if (empty) empty.remove();
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // --- message bubbles -----------------------------------------------------

  function buildProposal(message) {
    var wrap = document.createElement("div");
    wrap.className = "chat-proposal";
    wrap.dataset.status = message.proposed_status || "";

    var actionP = document.createElement("p");
    actionP.innerHTML = "<strong>Đề xuất hành động:</strong> ";
    var actionCode = document.createElement("code");
    actionCode.textContent = message.proposed_action_id;
    actionP.appendChild(actionCode);
    wrap.appendChild(actionP);

    var nodesP = document.createElement("p");
    var nodesStrong = document.createElement("strong");
    nodesStrong.textContent = "Node: ";
    nodesP.appendChild(nodesStrong);
    nodesP.appendChild(document.createTextNode((message.proposed_target_nodes || []).join(", ") || "—"));
    wrap.appendChild(nodesP);

    var rationaleP = document.createElement("p");
    var rationaleStrong = document.createElement("strong");
    rationaleStrong.textContent = "Lý do: ";
    rationaleP.appendChild(rationaleStrong);
    rationaleP.appendChild(document.createTextNode(message.proposed_rationale || "—"));
    wrap.appendChild(rationaleP);

    var cmdP = document.createElement("p");
    var cmdStrong = document.createElement("strong");
    cmdStrong.textContent = "Lệnh sẽ chạy: ";
    cmdP.appendChild(cmdStrong);
    if (message.proposed_command_preview) {
      var cmdCode = document.createElement("code");
      cmdCode.textContent = message.proposed_command_preview;
      cmdP.appendChild(cmdCode);
    } else {
      var hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = "Không có lệnh tự động — cần xử lý thủ công nếu xác nhận.";
      cmdP.appendChild(hint);
    }
    wrap.appendChild(cmdP);

    var actionsDiv = document.createElement("div");
    actionsDiv.className = "chat-proposal-actions";
    if (message.proposed_status === "PENDING") {
      if (message.proposed_action_id === "execute_node_command") {
        var okHint = document.createElement("strong");
        okHint.textContent = "Nhập chính xác OK ở tin nhắn kế tiếp để thực hiện.";
        actionsDiv.appendChild(okHint);
      } else {
        var btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-approve btn-sm chat-confirm-btn";
        btn.dataset.messageId = message.id;
        btn.textContent = "Thực hiện";
        actionsDiv.appendChild(btn);
      }
    } else if (message.proposed_status === "CONFIRMED") {
      var span = document.createElement("span");
      span.className = "chat-proposal-confirmed";
      span.textContent = "✓ Đã tạo yêu cầu — xem tiến trình ở Dashboard";
      actionsDiv.appendChild(span);
    } else if (message.proposed_status === "CANCELLED") {
      var cancelled = document.createElement("span");
      cancelled.className = "hint";
      cancelled.textContent = "Đã huỷ — tin nhắn kế tiếp không phải OK.";
      actionsDiv.appendChild(cancelled);
    }
    wrap.appendChild(actionsDiv);

    return wrap;
  }

  function buildMissingAiConfigNotice() {
    var p = document.createElement("p");
    p.className = "chat-msg-bubble";
    p.appendChild(document.createTextNode("⚙️ Chưa kết nối AI. "));
    var link = document.createElement("a");
    link.href = settingsUrl;
    link.textContent = "Vào Settings →";
    p.appendChild(link);
    return p;
  }

  // Click toggles a small tooltip listing the same tools again, in the
  // exact order they were called (tools_used is append-only, in call
  // order — see dashboard/chat_client.py::run_chat_turn) — the badge text
  // itself only shows a comma-joined summary, useful when the same tool
  // was called more than once in a turn (a plain Set-like summary would
  // hide that repetition, the ordered list doesn't).
  function buildToolsUsedBadge(toolsUsed) {
    var wrap = document.createElement("div");
    wrap.className = "chat-tools-badge-wrap";

    var badge = document.createElement("button");
    badge.type = "button";
    badge.className = "chat-tools-badge";
    badge.textContent = "🔧 Đã dùng: " + toolsUsed.join(", ");
    badge.setAttribute("aria-expanded", "false");

    var tooltip = document.createElement("ol");
    tooltip.className = "chat-tools-tooltip";
    tooltip.hidden = true;
    toolsUsed.forEach(function (name) {
      var li = document.createElement("li");
      li.textContent = name;
      tooltip.appendChild(li);
    });

    badge.addEventListener("click", function () {
      var willShow = tooltip.hidden;
      tooltip.hidden = !willShow;
      badge.setAttribute("aria-expanded", willShow ? "true" : "false");
    });

    wrap.appendChild(badge);
    wrap.appendChild(tooltip);
    return wrap;
  }

  function buildMessage(message) {
    var isUser = message.role === "user";
    var container = document.createElement("div");
    container.className = "chat-msg " + (isUser ? "chat-msg-user" : "chat-msg-assistant");
    container.dataset.messageId = message.id;

    var meta = document.createElement("div");
    meta.className = "chat-msg-meta";
    meta.textContent = isUser
      ? "Bạn · " + (message.actor || "?") + " · " + formatTimestamp(message.created_at)
      : "🤖 " + aiName + " · " + formatTimestamp(message.created_at);
    container.appendChild(meta);

    var bubble;
    if (!isUser && message.content.indexOf(MISSING_AI_CONFIG_MESSAGE) !== -1) {
      bubble = buildMissingAiConfigNotice();
    } else {
      bubble = document.createElement("div");
      bubble.className = "chat-msg-bubble";
      bubble.textContent = message.content;
    }
    container.appendChild(bubble);

    if (message.proposed_action_id) {
      bubble.appendChild(buildProposal(message));
    }
    if (!isUser && message.tools_used && message.tools_used.length) {
      container.appendChild(buildToolsUsedBadge(message.tools_used));
    }

    return container;
  }

  function appendMessage(message) {
    clearEmptyState();
    messagesEl.appendChild(buildMessage(message));
    scrollToBottom();
  }

  function replaceMessage(message) {
    var existing = messagesEl.querySelector('[data-message-id="' + message.id + '"]');
    var rebuilt = buildMessage(message);
    if (existing) {
      existing.replaceWith(rebuilt);
    } else {
      messagesEl.appendChild(rebuilt);
    }
  }

  // --- typing indicator ------------------------------------------------------

  function showTypingIndicator() {
    clearEmptyState();
    var container = document.createElement("div");
    container.className = "chat-msg chat-msg-assistant";
    container.id = TYPING_ID;

    var meta = document.createElement("div");
    meta.className = "chat-msg-meta";
    meta.textContent = "🤖 " + aiName;
    container.appendChild(meta);

    var bubble = document.createElement("div");
    bubble.className = "chat-msg-bubble";
    var dots = document.createElement("span");
    dots.className = "chat-typing-dots";
    dots.innerHTML = "<span></span><span></span><span></span>";
    bubble.appendChild(dots);
    container.appendChild(bubble);

    messagesEl.appendChild(container);
    scrollToBottom();
  }

  function removeTypingIndicator() {
    var el = document.getElementById(TYPING_ID);
    if (el) el.remove();
  }

  // --- error line -----------------------------------------------------------

  function showError(text) {
    errorEl.hidden = false;
    errorEl.textContent = text;
  }

  function clearError() {
    errorEl.hidden = true;
    errorEl.textContent = "";
  }

  // --- history load -----------------------------------------------------------

  function loadHistory() {
    fetch(apiPrefix + "/messages", { credentials: "same-origin" })
      .then(handleAuthRedirect)
      .then(function (response) { return response.json(); })
      .then(function (data) {
        currentSessionId = data.session_id || null;
        var messages = data.messages || [];
        if (!messages.length) return;
        clearEmptyState();
        messages.forEach(function (message) { messagesEl.appendChild(buildMessage(message)); });
        scrollToBottom();
      })
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
      });
  }

  function loadPreferences() {
    return fetch(apiPrefix + "/preferences", { credentials: "same-origin" })
      .then(handleAuthRedirect)
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        aiName = data.ai_name || "AI";
        if (aiNameInputEl) aiNameInputEl.value = aiName;
        if (panelAiNameEl) panelAiNameEl.textContent = aiName;
      });
  }

  loadPreferences()
    .catch(function () {})
    .then(loadHistory);

  // --- new session (Đoạn chat mới) --------------------------------------------

  function resetToEmptyState() {
    while (messagesEl.firstChild) messagesEl.removeChild(messagesEl.firstChild);
    var empty = document.createElement("div");
    empty.className = "chat-empty-state";
    empty.id = "chat-empty-state";
    empty.innerHTML =
      '<span class="chat-empty-icon" aria-hidden="true">&#129302;</span>' +
      '<p class="chat-empty-text">Hỏi tôi về ' + productName + '</p>' +
      '<p class="chat-empty-subtext">Trợ lý có thể giải thích cấu hình, vận hành và sự cố.</p>';
    messagesEl.appendChild(empty);
  }

  function startNewSession() {
    clearError();
    fetch(apiPrefix + "/sessions", { method: "POST", credentials: "same-origin" })
      .then(handleAuthRedirect)
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        currentSessionId = data.session_id;
        resetToEmptyState();
        if (panelEl.classList.contains("is-minimized")) setMinimized(false);
        inputEl.focus();
      })
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
        showError(err instanceof TypeError ? NETWORK_ERROR_MESSAGE : err.message);
      });
  }

  if (newSessionBtn) {
    newSessionBtn.addEventListener("click", startNewSession);
  }

  // --- session history (list / delete past sessions) --------------------------
  //
  // Three view modes, tracked in `historyMode`:
  //   "closed" — normal live chat (.chat-messages + .chat-form visible)
  //   "list"   — browsing past sessions (.chat-history-list-view visible),
  //              each row deletable but not openable — no read-only
  //              transcript view of a past session exists in this UI.

  var historyMode = "closed";

  function applyViewMode(mode) {
    historyMode = mode;
    historyListViewEl.hidden = mode !== "list";
    if (settingsViewEl) settingsViewEl.hidden = mode !== "settings";
    messagesEl.hidden = mode !== "closed";
    formEl.hidden = mode !== "closed";
    if (historyBtn) {
      historyBtn.classList.toggle("is-active", mode === "list");
      historyBtn.setAttribute("aria-pressed", mode === "list" ? "true" : "false");
    }
    if (settingsBtn) {
      settingsBtn.classList.toggle("is-active", mode === "settings");
      settingsBtn.setAttribute("aria-pressed", mode === "settings" ? "true" : "false");
    }
  }

  function buildHistoryRow(entry) {
    var row = document.createElement("div");
    row.className = "chat-history-row" + (entry.is_current ? " is-current" : "");
    row.dataset.sessionId = entry.session_id || "";

    var main = document.createElement("div");
    main.className = "chat-history-row-main";
    var preview = document.createElement("p");
    preview.className = "chat-history-row-preview";
    preview.textContent = entry.preview;
    main.appendChild(preview);
    var meta = document.createElement("p");
    meta.className = "chat-history-row-meta";
    meta.textContent =
      entry.message_count + " tin nhắn · " + formatDateTime(entry.last_active_at) +
      (entry.is_current ? " · hiện tại" : "");
    main.appendChild(meta);
    row.appendChild(main);

    // A session with no session_id (legacy pre-migration data) can't be
    // addressed by the view/delete-by-id endpoints — shown for visibility,
    // just not actionable.
    if (entry.session_id) {
      var delBtn = document.createElement("button");
      delBtn.type = "button";
      delBtn.className = "chat-history-delete-btn";
      delBtn.dataset.sessionId = entry.session_id;
      delBtn.setAttribute("aria-label", "Xoá đoạn chat này");
      delBtn.title = "Xoá đoạn chat này";
      delBtn.textContent = "🗑"; // 🗑
      row.appendChild(delBtn);
    }

    return row;
  }

  function renderHistoryList(sessions) {
    while (historyListEl.firstChild) historyListEl.removeChild(historyListEl.firstChild);
    if (!sessions.length) {
      var empty = document.createElement("p");
      empty.className = "chat-history-empty";
      empty.textContent = "Chưa có đoạn chat nào.";
      historyListEl.appendChild(empty);
      return;
    }
    sessions.forEach(function (entry) { historyListEl.appendChild(buildHistoryRow(entry)); });
  }

  function openHistoryList() {
    clearError();
    fetch(apiPrefix + "/sessions", { credentials: "same-origin" })
      .then(handleAuthRedirect)
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function (data) {
        renderHistoryList(data.sessions || []);
        applyViewMode("list");
      })
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
        showError(err instanceof TypeError ? NETWORK_ERROR_MESSAGE : err.message);
      });
  }

  function closeHistoryView() {
    applyViewMode("closed");
    // Reload whatever's actually current — it may have changed (a new
    // message sent elsewhere, or a session deleted) while browsing history.
    resetToEmptyState();
    loadHistory();
  }

  function deleteHistorySession(sessionId, rowEl) {
    if (!window.confirm("Xoá vĩnh viễn đoạn chat này? Không thể hoàn tác.")) return;
    fetch(apiPrefix + "/sessions/" + encodeURIComponent(sessionId), {
      method: "DELETE",
      credentials: "same-origin",
    })
      .then(handleAuthRedirect)
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(function () {
        var wasCurrent = rowEl.classList.contains("is-current");
        rowEl.remove();
        if (!historyListEl.firstChild) {
          var empty = document.createElement("p");
          empty.className = "chat-history-empty";
          empty.textContent = "Chưa có đoạn chat nào.";
          historyListEl.appendChild(empty);
        }
        if (wasCurrent) {
          // The live view (if reopened) must not keep pointing at a
          // session that no longer exists — closeHistoryView()'s
          // loadHistory() call will resolve whatever's now current, or
          // the empty state if nothing's left.
          currentSessionId = null;
        }
      })
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
        showError(err instanceof TypeError ? NETWORK_ERROR_MESSAGE : err.message);
      });
  }

  if (historyBtn) {
    historyBtn.addEventListener("click", function () {
      // "closed" -> open the list. "list" -> toggle closed (back to live
      // chat).
      if (historyMode === "list") {
        closeHistoryView();
      } else {
        openHistoryList();
      }
    });
  }
  if (settingsBtn) {
    settingsBtn.addEventListener("click", function () {
      clearError();
      if (historyMode === "settings") {
        closeHistoryView();
        return;
      }
      if (settingsSuccessEl) settingsSuccessEl.hidden = true;
      loadPreferences()
        .then(function () {
          applyViewMode("settings");
          if (aiNameInputEl) aiNameInputEl.focus();
        })
        .catch(function (err) {
          if (err.message !== "unauthenticated") showError(err.message);
        });
    });
  }
  if (settingsFormEl) {
    settingsFormEl.addEventListener("submit", function (event) {
      event.preventDefault();
      clearError();
      var submitBtn = settingsFormEl.querySelector('button[type="submit"]');
      if (submitBtn) {
        submitBtn.disabled = true;
        submitBtn.textContent = "Đang lưu…";
      }
      fetch(apiPrefix + "/preferences", {
        method: "PUT",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ai_name: aiNameInputEl.value })
      })
        .then(handleAuthRedirect)
        .then(function (response) {
          return response.json().then(function (data) {
            if (!response.ok) throw new Error(data.detail || "HTTP " + response.status);
            return data;
          });
        })
        .then(function (data) {
          aiName = data.ai_name;
          aiNameInputEl.value = aiName;
          if (panelAiNameEl) panelAiNameEl.textContent = aiName;
          if (settingsSuccessEl) {
            settingsSuccessEl.textContent = "Đã lưu tên " + aiName + ".";
            settingsSuccessEl.hidden = false;
          }
        })
        .catch(function (err) {
          if (err.message !== "unauthenticated") showError(err.message);
        })
        .finally(function () {
          if (submitBtn) {
            submitBtn.disabled = false;
            submitBtn.textContent = "Lưu thay đổi";
          }
        });
    });
  }
  historyListEl.addEventListener("click", function (event) {
    var delBtn = event.target.closest(".chat-history-delete-btn");
    if (delBtn) {
      deleteHistorySession(delBtn.dataset.sessionId, delBtn.closest(".chat-history-row"));
    }
  });

  // --- minimize / restore (thu nhỏ / phóng to) --------------------------------

  function setMinimized(minimized) {
    panelEl.classList.toggle("is-minimized", minimized);
    if (minimizeBtn) {
      minimizeBtn.textContent = minimized ? RESTORE_ICON : MINIMIZE_ICON;
      minimizeBtn.setAttribute("aria-label", minimized ? "Phóng to" : "Thu nhỏ");
      minimizeBtn.title = minimized ? "Phóng to" : "Thu nhỏ";
    }
    try {
      localStorage.setItem(MINIMIZED_STORAGE_KEY, minimized ? "1" : "0");
    } catch (e) {
      // localStorage unavailable (private mode, quota) — state just won't
      // persist across reloads, not worth failing the toggle over.
    }
    if (!minimized) {
      scrollToBottom();
    }
  }

  if (minimizeBtn) {
    minimizeBtn.addEventListener("click", function () {
      setMinimized(!panelEl.classList.contains("is-minimized"));
    });
  }
  if (closeBtn) {
    // No separate "fully closed" state exists — this panel is part of the
    // page layout, not a floating widget with its own reopen affordance
    // elsewhere on the page, so × collapses it the same way − does. Two
    // buttons are kept (matches the header mockup) since some operators
    // reach for × out of habit.
    closeBtn.addEventListener("click", function () { setMinimized(true); });
  }

  var startMinimized = false;
  try {
    startMinimized = localStorage.getItem(MINIMIZED_STORAGE_KEY) === "1";
  } catch (e) {
    startMinimized = false;
  }
  setMinimized(startMinimized);

  // --- textarea auto-resize + send-button enabled state -----------------------

  function autoResizeTextarea() {
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 120) + "px";
  }

  function refreshSendEnabled() {
    sendBtn.disabled = inputEl.value.trim().length === 0;
  }

  inputEl.addEventListener("input", function () {
    autoResizeTextarea();
    refreshSendEnabled();
  });
  refreshSendEnabled();

  inputEl.addEventListener("keydown", function (event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      if (!sendBtn.disabled) formEl.requestSubmit();
    }
  });

  // --- sending messages ---------------------------------------------------

  formEl.addEventListener("submit", function (event) {
    event.preventDefault();
    var text = inputEl.value.trim();
    if (!text) return;

    clearError();
    inputEl.value = "";
    autoResizeTextarea();
    inputEl.disabled = true;
    sendBtn.disabled = true;
    // Shown immediately — the POST below blocks until Claude's full reply
    // is ready (possibly several tool round trips), so this is the actual
    // wait, not a fixed-duration decoration.
    showTypingIndicator();

    fetch(apiPrefix + "/messages", {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text, session_id: currentSessionId }),
    })
      .then(handleAuthRedirect)
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (data) {
            throw new Error(data.detail || "HTTP " + response.status);
          });
        }
        return response.json();
      })
      .then(function (data) {
        removeTypingIndicator();
        // Syncs currentSessionId even on a very first message (sent with
        // currentSessionId still null) — the backend generates one in that
        // case, and every message from here on must carry it.
        currentSessionId = data.user_message.session_id || currentSessionId;
        appendMessage(data.user_message);
        appendMessage(data.assistant_message);
      })
      .catch(function (err) {
        removeTypingIndicator();
        if (err.message === "unauthenticated") return;
        // fetch() itself rejects with a TypeError for a genuine network
        // failure (offline, DNS, connection refused) — distinct from the
        // Error this code throws above for a non-2xx HTTP response, which
        // already carries the server's own (more specific) detail message.
        showError(err instanceof TypeError ? NETWORK_ERROR_MESSAGE : err.message);
      })
      .finally(function () {
        inputEl.disabled = false;
        refreshSendEnabled();
        inputEl.focus();
      });
  });

  // --- confirm a staged proposal ------------------------------------------

  messagesEl.addEventListener("click", function (event) {
    var btn = event.target.closest(".chat-confirm-btn");
    if (!btn) return;
    var messageId = btn.dataset.messageId;
    btn.disabled = true;
    btn.textContent = "Đang xử lý...";

    fetch(apiPrefix + "/messages/" + encodeURIComponent(messageId) + "/confirm-action", {
      method: "POST",
      credentials: "same-origin",
    })
      .then(handleAuthRedirect)
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (data) {
            throw new Error(data.detail || "HTTP " + response.status);
          });
        }
        return response.json();
      })
      .then(function (message) { replaceMessage(message); })
      .catch(function (err) {
        if (err.message === "unauthenticated") return;
        showError(err instanceof TypeError ? NETWORK_ERROR_MESSAGE : err.message);
        btn.disabled = false;
        btn.textContent = "Thực hiện";
      });
  });
})();
