# Realtime mutation inventory

Scope: Ceph-AI dashboard on `10.3.55.213`.

This inventory is the input for RT-08. A mutation is allowed to publish a
successful realtime invalidation only after the Worker/executor has committed
the action state and the post-check confirms the cluster state.

## Event contract

| Event | Sections | When it may be emitted |
| --- | --- | --- |
| `action_state_changed` | none or action-scoped | After the durable Action transition is committed. It represents lifecycle progress, not Ceph success. |
| `snapshot_changed` | `health`, `status`, `pools`, `pgs`, `crush`, `nodes` | After a snapshot/section is persisted successfully. |
| `snapshot_refresh_failed` | affected sections | After a refresh attempt fails while the previous snapshot is retained. |

The browser must refetch the authoritative snapshot after an event. Event
payloads must not contain commands, credentials, keyrings, SSH paths, or full
Ceph inventory payloads.

## Mutation matrix

| Area | Entry points | Expected state/event sections | Post-check owner |
| --- | --- | --- | --- |
| Pool/PG | `dashboard/routes/pgs.py` pool create/action | `pools`, `pgs`, possibly `health` | Worker action verifier |
| RBD volume | `dashboard/routes/volumes.py` create, resize, rename, clone, flatten, trash, restore, snapshots, QoS | `pools`, `pgs`, `health` | Worker action verifier |
| CRUSH/OSD | CRUSH routes and action-policy commands | `crush`, `nodes`, `health` | Worker post-check / incident resolver |
| Cluster lifecycle | `dashboard/routes/deploy_cluster.py`, `delete_cluster.py`, `restore_cluster.py`, `convert_cluster.py` | `health`, `status`, `nodes`, `pools`, `crush` | deployment operation verifier |
| Upgrade | `dashboard/routes/upgrade.py` | `health`, `status`, `nodes` | upgrade gate/post-check |
| Backup/restore | `dashboard/routes/backups.py`, `cinder_backups.py` | `pools`, `pgs`, `health` when Ceph state changes | Worker action verifier |
| RGW/Object Storage | object-storage and bucket/user action routes | future `rgw`, plus `health`/`status` when applicable | Worker action verifier |
| Patch pipeline | `dashboard/routes/patch.py` | `nodes`, `status`, `health` after installation/restart | patch executor/post-check |
| Action lifecycle | `dashboard/routes/actions.py`, `vitastor_actions.py` | no snapshot section by itself | DB action-state commit hook |
| Cluster configuration | `dashboard/routes/settings.py`, OpenStack/Ceph config routes | affected section only after service reload/post-check | config operation verifier |

## Explicitly excluded from cluster-state invalidation

User accounts, dashboard login, AI model settings, Telegram channel settings,
runbook feedback, and read-only analysis jobs do not invalidate Ceph cluster
snapshots. They may emit their own UI toast or job-progress event later.

## Implementation order

1. Keep `action_state_changed` as lifecycle-only (`queued`, `running`,
   `verifying`, `succeeded`, `failed`).
2. Map each action family to bounded snapshot sections.
3. Emit `snapshot_changed` only from the post-check success path.
4. Emit `snapshot_refresh_failed` while retaining the last valid snapshot.
5. Add route/worker tests for one success, one failed post-check, duplicate
   event, and wrong-cluster isolation per family.
