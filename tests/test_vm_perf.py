import json
from types import SimpleNamespace

from worker.executor import vm_perf


def _fio_output(iops=1000.0, avg_ns=1_000_000, p99_ns=2_000_000):
    return json.dumps(
        {
            "jobs": [
                {
                    "read": {
                        "iops": iops,
                        "bw_bytes": iops * 4096,
                        "clat_ns": {
                            "mean": avg_ns,
                            "percentile": {"99.000000": p99_ns},
                        },
                    }
                }
            ]
        }
    )


def test_run_executes_read_only_fio_inside_vm(monkeypatch):
    calls = []
    progress_updates = []

    def fake_execute(host, command, user=None, key_path=None):
        calls.append((host, command, user, key_path))
        if "lsblk" in command:
            return "vdb 100G disk 0\n"
        return _fio_output()

    monkeypatch.setattr(vm_perf, "execute_command", fake_execute)
    monkeypatch.setattr(vm_perf, "IODEPTH_STEPS", (1, 4))
    monkeypatch.setattr(vm_perf, "FIO_SAMPLES_PER_DEPTH", 2)

    result = vm_perf.run(
        "action-1",
        {
            "controller_ip": "10.0.0.10",
            "vm_ip": "10.20.1.50",
            "ssh_user": "ubuntu",
            "ssh_key_path": "/keys/vm",
            "device": "/dev/vdb",
        },
        "incident-1",
        lambda _action, progress: progress_updates.append(json.loads(json.dumps(progress))),
        SimpleNamespace(ssh_user="ceph-admin", ssh_key_path="/keys/controller"),
    )

    assert result is True
    fio_calls = [call for call in calls if "fio --name" in call[1]]
    assert len(fio_calls) == 4
    assert all("sudo -n fio --name" in call[1] for call in fio_calls)
    assert all("--readonly" in call[1] and "--rw=randread" in call[1] for call in fio_calls)
    assert all(call[0] == "10.0.0.10" for call in calls)
    assert all(call[2:] == ("ceph-admin", "/keys/controller") for call in calls)
    assert all("ssh -i /keys/vm" in call[1] and "ubuntu@10.20.1.50" in call[1] for call in calls)
    final = progress_updates[-1][-1]
    assert final["status"] == "done"
    assert final["result"]["device"] == "/dev/vdb"
    assert len(final["result"]["steps"]) == 2

    prepare_call = next(call for call in calls if "lsblk" in call[1])
    assert "sudo -n true" in prepare_call[1]
    assert "sudo -n test -b /dev/vdb" in prepare_call[1]
    assert "sudo -n lsblk" in prepare_call[1]


def test_max_volume_performance_runs_three_samples_per_depth():
    assert vm_perf.FIO_SAMPLES_PER_DEPTH == 3


def test_run_rejects_unsafe_device_without_ssh(monkeypatch):
    monkeypatch.setattr(
        vm_perf,
        "execute_command",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not SSH")),
    )
    assert vm_perf.run(
        "action-2",
        {
            "controller_ip": "10.0.0.10",
            "vm_ip": "10.20.1.50",
            "ssh_user": "root",
            "ssh_key_path": "/key",
            "device": "/dev/vdb;reboot",
        },
        "incident-2",
        lambda *_: None,
    ) is False
