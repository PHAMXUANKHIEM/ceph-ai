# Vitastor monitoring

Vitastor monitoring is independent from the Ceph incident/action pipeline. The Watcher polls every active `VitastorCluster`, stores 30 days of time-series samples, updates the Dashboard cache, and emits transition-only Telegram alerts.

## Data sources

- `vitastor-cli status`, `ls-pools --stats`, `osd-tree -l`, and `ls -l`: cluster, pool, PG, image, recovery, and per-OSD I/O.
- `etcdctl endpoint status/health`: member connectivity, quorum, leader, database size, and endpoint latency.
- `ps`, `smartctl`, `lsblk`, and `/sys/class/net`: OSD CPU/RAM, device health, NIC counters, MTU, and link speed.
- `ping`: OSD-to-OSD and management/client-path RTT plus optional Jumbo 9000 validation.

Install `etcdctl`, `smartmontools`, `iputils-ping`, and `util-linux` on monitored hosts. The configured Vitastor SSH account needs read access to process, sysfs, and SMART data. SMART commonly requires root or a narrowly scoped sudo rule.

## Default thresholds

| Signal | Warning | Critical |
|---|---:|---:|
| Capacity | 85% | 90% |
| Etcd latency | 100 ms | 500 ms |
| Recovery bandwidth | 500 MB/s | 1000 MB/s |
| Disk temperature | 65°C | 75°C |
| Device wear used | 80% | 95% |
| Network RTT | 5 ms | 20 ms |

Slow OSD detection requires three consecutive samples, at least 20 ms latency, and at least three times the median OSD latency. Media errors, a failed SMART status, lost etcd quorum, missing/duplicate leader, and an unreachable network path are critical.

All defaults can be overridden through the matching uppercase environment variable, for example `VITASTOR_CAPACITY_WARNING_PERCENT=80`. Set `VITASTOR_EXPECT_JUMBO_FRAMES=true` only when the storage network is intentionally configured for MTU 9000; otherwise a failed Jumbo probe is displayed but does not alert.

## Alert behavior

Alerts are sent only when an entity changes state, including escalation and recovery. NIC counters alert only when errors or drops increase between samples, not merely because a cumulative counter is non-zero. A failure to collect optional etcd, SMART, or network detail is isolated from the native Vitastor cluster-health result.
