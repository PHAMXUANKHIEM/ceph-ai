# Learning safeguards

Phase 6.4 keeps predictive learning bounded and fail-closed:

- learning work is rate-limited per volume and has a configurable maximum
  batch size;
- Loki calls use an explicit timeout, finite retries, and a per-process
  circuit breaker;
- host/volume raw samples, forecast runs, terminal forecast evidence, and
  old learning audit rows are pruned on a bounded schedule;
- active alerts, model state, open operator work, and active model registry
  rows are not removed by retention.

Before applying an Alembic migration, create a database backup:

```bash
scripts/deploy/backup_database_before_migration.sh /var/backups/ceph-ai
alembic upgrade head
```

The backup script supports PostgreSQL (`pg_dump --format=custom`) and SQLite
(`sqlite3 .backup`). Store the resulting file outside the application data
directory and verify that it is readable before proceeding.
