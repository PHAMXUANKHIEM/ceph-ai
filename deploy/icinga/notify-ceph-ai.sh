#!/bin/sh
# Icinga NotificationCommand "ceph-ai-webhook": POST one service event to Ceph AI (plan icinga I1).
# Settings in /etc/icinga2/ceph-ai/webhook.env (0640 root:nagios): CEPH_AI_URL, CEPH_AI_TOKEN.
# The event fields arrive as environment variables (ceph-ai.conf); python3 builds the JSON so no
# value is ever interpolated into a shell or JSON string by hand.
set -eu
. /etc/icinga2/ceph-ai/webhook.env
python3 - <<'PY' | curl -fsS --max-time 15 -H "Authorization: Bearer ${CEPH_AI_TOKEN}" \
  -H "Content-Type: application/json" --data-binary @- "${CEPH_AI_URL%/}/api/icinga/events" >/dev/null
import json, os, hashlib
event = {key.lower(): os.environ.get(key, "") for key in (
    "NOTIFICATION_TYPE", "HOST", "SERVICE", "STATE", "STATE_TYPE", "OUTPUT", "LONG_OUTPUT",
    "LAST_STATE_CHANGE", "AUTHOR", "COMMENT")}
seed = "|".join(event[key] for key in ("host", "service", "notification_type", "state", "last_state_change"))
event["event_id"] = hashlib.sha256(seed.encode()).hexdigest()[:32]   # idempotency key for retries
print(json.dumps(event))
PY
