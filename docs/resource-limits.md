# Ceph-AI resource safety policy

The production host has 8 logical CPUs and about 31 GiB RAM.  The Compose
stack now has hard service limits whose CPU total is 6.0 CPUs:

- Worker: 2 CPUs, 2 GiB RAM.
- Watcher: 1.5 CPUs, 1 GiB RAM.
- Full executor/job service: 1 CPU, 2 GiB RAM.
- Dashboard: 0.5 CPU, 512 MiB RAM.
- Telegram AI: 0.5 CPU, 1 GiB RAM.
- Code repair: 0.25 CPU, 1 GiB RAM.
- Vault monitor: 0.25 CPU, 256 MiB RAM.

This leaves at least 2 CPUs for the host OS, Ceph daemons, SSH and short-lived
maintenance commands.  `memswap_limit` equals `mem_limit`, so normal service
work is not allowed to spill into swap and cause latency or lockup pressure.

The two fio benchmark paths add `--cpus_allowed=0` and
`--cpus_allowed_policy=split`, so each benchmark job is limited to one CPU at
the target VM/Ceph host.  The existing online-learning code is lightweight
statistics, not continuous neural-model training; it remains inside the
Watcher 1.5-CPU cap.  Future training/replay jobs must use the 1-CPU job
budget rather than running in the Watcher poll loop.

The current host has no swap.  `scripts/deploy/check_resource_budget.sh` is a
read-only preflight and reports this as a warning.  The separate
`scripts/deploy/enable_safe_swap.sh` is an explicit operator action for a
4–8 GiB safety buffer, with `vm.swappiness=10`; it is not called by deploy or
restart automation.  Swap is an emergency buffer, not a normal model-runtime
budget.
