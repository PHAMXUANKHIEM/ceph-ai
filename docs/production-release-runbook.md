# Ceph AI production release runbook

This runbook is the operator-facing companion to the strict production
readiness plan. It applies to a release candidate built from one immutable
source SHA and one image digest.

> **Availability:** single node, no HA. See the availability model in
> [`operations/reliability-slo.md`](operations/reliability-slo.md) before
> promising recovery times to anyone.

## Release identity

Before a release, generate the manifest and documentation report:

```bash
python scripts/ci/documentation_freshness.py \
  --output artifacts/release/documentation-freshness.json
python scripts/ci/release_manifest.py \
  --environment staging \
  --output artifacts/release/release-manifest.json
```

The release is not eligible when the manifest reports a dirty worktree,
multiple Alembic heads, a missing image digest, expired documentation, or
missing required evidence.

## Required sequence

1. Run deploy preflight and save its redacted JSON/log output.
2. Verify the source SHA, image digest, SBOM, dependency lock hash and
   migration head.
3. Create and verify a PostgreSQL backup before migration.
4. Run the staging migration rehearsal against PostgreSQL, not SQLite.
5. Apply the migration only after the rehearsal and backup checks pass.
6. Restart services idempotently and verify Dashboard, Watcher, Worker,
   RabbitMQ consumer and migration head.
7. Run authenticated read-only smoke tests on the target host.
8. Record phase logs, running image digests, health evidence and operator
   witness in the release artifact.

For an isolated PostgreSQL restore rehearsal, use an explicit source and a
different staging target. The command rejects production target IDs and
identical source/target endpoints:

```bash
python scripts/deploy/postgresql_restore_rehearsal.py \
  --source-url "$STAGING_SOURCE_DATABASE_URL" \
  --source-id staging-source-20260925 \
  --target-url "$STAGING_RESTORE_DATABASE_URL" \
  --target-id staging-restore-20260925 \
  --confirm I_UNDERSTAND_STAGING_ONLY \
  --backup-dir /var/lib/ceph-ai/rehearsal-backups \
  --report artifacts/postgresql-restore-rehearsal.json
```

The report is evidence for review, not a production approval. Run failure
injection with `--inject-failure before_restore`, `after_restore`,
`before_migration` or `after_migration` only against the disposable target.

## Post-deploy smoke

`scripts/deploy/post_deploy_smoke.py` runs on the target host at the end of
every deploy and writes `deploy-evidence/post-deploy-smoke.json`. It fails the
deploy when the login page, the Watcher/Worker heartbeats, the database
migration head or the release SHA of any service image is wrong.

The authenticated dashboard check is `SKIPPED` until a dedicated low-privilege
smoke account exists. Create one in the Dashboard, then store it on the target
host (never in Git or in CI secrets that reach GitHub-hosted runners):

```text
install -m 0600 /dev/null /var/lib/ceph-ai/config/smoke-credentials
printf 'smoke-user:<password>\n' > /var/lib/ceph-ai/config/smoke-credentials
```

## Browser acceptance

`scripts/browser_acceptance.mjs` opens real Chromium pages (1280/1440/1920 px
and a 390 px phone) after logging in through the form, and checks HTTP status,
JavaScript errors and horizontal overflow on the main pages, anonymous access
being sent to `/login`, unknown data not rendered as `0/0`, `?cluster=`
switching, recovery after going offline, and keyboard focus. It writes
`browser-acceptance.json` and one screenshot per page and viewport to
`OUT_DIR/<sha>/`. It only reads; it never submits an action.

```text
BASE_URL=http://127.0.0.1:8000 DASHBOARD_PASSWORD=<operator password> \
SECOND_CLUSTER_ID=<id of a non-default cluster> GIT_SHA=$(git rev-parse --short HEAD) \
PLAYWRIGHT_MODULE=ceph-health-dashboard/node_modules/playwright/index.mjs \
/opt/ceph-ai-node20/bin/node scripts/browser_acceptance.mjs
```

Run it against an instance whose Ceph is unreachable as well, so the
unknown/stale rendering is exercised.

## Rollback

Container rollback and database rollback are separate operations. A failed
health check may roll back the immutable application image only when the
database schema remains compatible. A migration rollback requires a reviewed
downgrade or forward-fix plan and an isolated PostgreSQL rehearsal.

Never reset the development worktree to perform a deployment rollback.
Rollback must select the previous immutable release checkout/image digest.

## AI safety gate

River v2 and other candidate models remain shadow-only until independent
verified outcomes, paired evaluation, operator approval, audit evidence and a
rollback rehearsal are present. The release process must not enable
autonomous remediation merely because unit tests or a benchmark fixture pass.

## Capability matrix trước khi có action

Preflight (`worker/preflight.py`) chặn mọi đề xuất có lệnh khi capability matrix
không có entry cho đúng Ceph major của cluster; đề xuất bị chặn trở thành
`investigate_manually` chờ duyệt (không còn FAILED rồi tạo lại). Sau khi nâng
Ceph major (ví dụ lên 20.2.x), kiểm tra `/capability-matrix` và duyệt entry cho
major mới trước khi trông đợi đề xuất thật. Seed AI cần router đã cấu hình; nếu
không, tạo proposal có trích dẫn nguyên văn từ docs.ceph.com và duyệt trên trang.

## Nâng quyền tự thực thi

Không bật tự thực thi cho một action chỉ vì test hoặc benchmark pass. Thứ tự bắt
buộc (autonomy plan WP6.4):

1. Shadow ít nhất 30 ngày: policy chỉ ghi khuyến nghị execute/escalate vào
   `autonomy_decisions`, không có đường nào từ shadow tới thực thi.
2. Đánh giá off-policy (`scripts/ope_report.py`, DR có khoảng tin cậy 95%) cho
   thấy false-release rate ≤ ngân sách (`SHADOW_FRR_BUDGET`) khi propensity đã đủ
   đa dạng; logging tất định (propensity 1.0) không đủ chứng minh.
3. Operator duyệt canary cho **1 action × 1 cluster**, có audit như promotion
   model registry.
4. Canary 14 ngày: theo dõi FRR thật, `regressed_24h`, số escalation; vi phạm thì
   tự hạ về APPROVAL_REQUIRED.
5. Kill switch và guardrail của autopilot luôn thắng lựa chọn của policy.

`AUTOPILOT_ENABLED` hiện chỉ cho các action được phân loại SAFE trong
`worker/policy/action_policy.yaml` (và ngoại lệ đã kiểm chứng như restart OSD cho
`BLUESTORE_SLOW_OP_ALERT` khi mọi `osd.N` đã ánh xạ được host). Thay đổi danh sách
SAFE là quyết định vận hành phải ghi lại trong release notes.

