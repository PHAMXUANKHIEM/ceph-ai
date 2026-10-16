#!/bin/sh
# Icinga NotificationCommand "telegram-service" (plan icinga I1, parallel run with Ceph AI's own alerts).
# Settings in /etc/icinga2/ceph-ai/telegram.env (0640 root:nagios): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID.
set -eu
. /etc/icinga2/ceph-ai/telegram.env
case "${STATE:-}" in OK) mark="✅" ;; WARNING) mark="⚠️" ;; CRITICAL) mark="🔴" ;; *) mark="❔" ;; esac
text="$(printf '%s [Icinga] %s %s/%s: %s\n%s' "$mark" "${NOTIFICATION_TYPE:-}" "${HOST:-}" "${SERVICE:-}" "${STATE:-}" "${OUTPUT:-}" | cut -c1-3500)"
curl -fsS --max-time 15 "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=${text}" \
  --data-urlencode "disable_web_page_preview=true" >/dev/null
