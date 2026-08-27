# Synthetic Incident Injection (lab)

The synthetic harness exercises the normal Incident → AI diagnosis → Action
and RemediationCase path without changing Ceph. It is deliberately
shadow-only: the Worker blocks both Autopilot and manually approved Actions
when the Incident carries the synthetic marker.

Only clusters whose `autonomy_environment` is explicitly `lab` are accepted.
The command never writes Ceph state; `--publish` only sends the marked
Incident envelope to the existing RabbitMQ queue.

```bash
cd /root/ceph-ai
.venv/bin/python -m scripts.lab.synthetic_incident --list
.venv/bin/python -m scripts.lab.synthetic_incident \
  --cluster-id <LAB_CLUSTER_UUID> --scenario osd_down --publish
```

The command prints an `incident_id` and `run_id`. Clean up only synthetic
rows after the run:

```bash
.venv/bin/python -m scripts.lab.synthetic_incident \
  --cluster-id <LAB_CLUSTER_UUID> --cleanup --run-id <RUN_ID>
```

Cleanup marks matching rows `REJECTED`; real Incidents are never touched.
Synthetic outcomes are not eligible to raise production Trust Engine scores.
For a real Ceph fault campaign, keep using the narrowly guarded
`scripts/lab/large_omap_training.sh` harness.
