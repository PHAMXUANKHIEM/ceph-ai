#!/usr/bin/env bash
set -euo pipefail

# End-to-end LARGE_OMAP_OBJECTS training drill.  This is intentionally
# impossible to run against a non-test bucket.
ADMIN_HOST="${1:-10.3.53.1}"
BUCKET="${2:-test-large-omap}"
POOL="${3:-us-east-1.rgw.buckets.index}"
TEST_THRESHOLD="${TEST_THRESHOLD:-5000}"
TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-600}"

if [[ ! "$BUCKET" =~ ^test-[a-zA-Z0-9._-]+$ ]]; then
  echo "REFUSED: bucket must start with test-" >&2
  exit 2
fi
if [[ ! "$ADMIN_HOST" =~ ^10\.3\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
  echo "REFUSED: ADMIN_HOST must be inside the 10.3.x.x lab network" >&2
  exit 2
fi

remote() { ssh -o BatchMode=yes -o ConnectTimeout=8 "root@$ADMIN_HOST" "$@"; }

original_threshold="$(remote "ceph config get osd osd_deep_scrub_large_omap_object_key_threshold")"
original_dynamic="$(remote "ceph config get client.rgw rgw_dynamic_resharding")"
restore() {
  remote "ceph config set osd osd_deep_scrub_large_omap_object_key_threshold '$original_threshold'; ceph config set client.rgw rgw_dynamic_resharding '$original_dynamic'" >/dev/null || true
}
trap restore EXIT INT TERM

stats="$(remote "radosgw-admin bucket stats --bucket='$BUCKET'")"
objects="$(printf '%s' "$stats" | sed -n 's/.*\"num_objects\": \([0-9]*\).*/\1/p' | head -1)"
if [[ -z "$objects" || "$objects" -le "$TEST_THRESHOLD" ]]; then
  echo "REFUSED: bucket has $objects objects; needs more than threshold $TEST_THRESHOLD" >&2
  exit 3
fi

echo "[inject] bucket=$BUCKET objects=$objects threshold=$TEST_THRESHOLD"
remote "ceph config set client.rgw rgw_dynamic_resharding false; ceph config set osd osd_deep_scrub_large_omap_object_key_threshold '$TEST_THRESHOLD'; radosgw-admin bucket reshard --bucket='$BUCKET' --num-shards=1 --yes-i-really-mean-it" >/dev/null

bucket_id="$(printf '%s' "$stats" | sed -n 's/.*\"id\": \"\([^\"]*\)\".*/\1/p' | head -1)"
evidence="$(remote "pool='$POOL'; id='$bucket_id'; best=''; max=-1; for obj in \$(rados -p \"\$pool\" ls | grep -F \".dir.\$id\"); do count=\$(rados -p \"\$pool\" listomapkeys \"\$obj\" | wc -l); if [ \"\$count\" -gt \"\$max\" ]; then best=\$obj; max=\$count; fi; done; map=\$(ceph osd map \"\$pool\" \"\$best\"); pg=\$(printf '%s\\n' \"\$map\" | sed -n 's/.*(\\([0-9][0-9]*\\.[0-9a-fA-F][0-9a-fA-F]*\\)).*/\\1/p'); printf '%s|%s|%s' \"\$best\" \"\$max\" \"\$pg\"")"
IFS='|' read -r index_object key_count pg_id <<<"$evidence"
if [[ -z "$index_object" || -z "$pg_id" || "$key_count" -le "$TEST_THRESHOLD" ]]; then
  echo "REFUSED: incomplete injection evidence: $evidence" >&2
  exit 4
fi
echo "[inject] object=$index_object keys=$key_count pg=$pg_id"
remote "ceph pg deep-scrub '$pg_id'" >/dev/null

deadline=$((SECONDS + TIMEOUT_SECONDS))
while (( SECONDS < deadline )); do
  if remote "ceph health detail" | grep -q LARGE_OMAP_OBJECTS; then
    echo "[detected] LARGE_OMAP_OBJECTS"
    break
  fi
  sleep 3
done
if (( SECONDS >= deadline )); then
  echo "FAILED: Ceph did not expose LARGE_OMAP_OBJECTS" >&2
  exit 5
fi

# From here no repair command is issued by this harness.  It only observes
# the AI worker's reshard + deep-scrub outcome.
while (( SECONDS < deadline )); do
  current_stats="$(remote "radosgw-admin bucket stats --bucket='$BUCKET'")"
  shards="$(printf '%s' "$current_stats" | sed -n 's/.*\"num_shards\": \([0-9]*\).*/\1/p' | head -1)"
  health="$(remote "ceph health detail")"
  if [[ "$shards" -gt 1 ]] && ! grep -q LARGE_OMAP_OBJECTS <<<"$health"; then
    break
  fi
  sleep 5
done
if (( SECONDS >= deadline )); then
  echo "FAILED: AI did not repair and clear the warning in time" >&2
  exit 6
fi

check="$(remote "radosgw-admin bucket check --bucket='$BUCKET' --check-objects=false")"
CHECK_JSON="$check" python3 - "$objects" "$shards" <<'PY'
import json, os, sys
expected, shards = int(sys.argv[1]), int(sys.argv[2])
data = json.loads(os.environ["CHECK_JSON"])["check_result"]
existing = data["existing_header"]["usage"]["rgw.main"]["num_objects"]
calculated = data["calculated_header"]["usage"]["rgw.main"]["num_objects"]
if existing != expected or calculated != expected or shards <= 1:
    raise SystemExit(f"FAILED integrity: expected={expected} existing={existing} calculated={calculated} shards={shards}")
print(f"PASSED objects={expected} existing={existing} calculated={calculated} shards={shards}")
PY
remote "ceph health detail"
