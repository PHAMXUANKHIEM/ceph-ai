import pytest

from shared import ceph_features as cf

# Shapes taken from the CS-LAB cluster (Squid, cephadm, virtio disks), redacted.
MODULES = {
    "always_on_modules": ["balancer", "crash", "devicehealth", "orchestrator", "pg_autoscaler", "status"],
    "enabled_modules": ["cephadm", "dashboard", "prometheus"],
    "disabled_modules": [{"name": "diskprediction_local", "can_run": True}, {"name": "influx", "can_run": False}],
}
DEVICES = [
    {"devid": "mon-disk-a", "location": [{"host": "ceph2", "dev": "vda"}], "daemons": ["mon.ceph2"]},
    {"devid": "osd-disk-b", "location": [{"host": "ceph3", "dev": "vdb"}], "daemons": ["osd.2"]},
    {"devid": "osd-disk-c", "location": [{"host": "ceph1", "dev": "vdb"}], "daemons": ["osd.0"],
     "life_expectancy_min": "2027-01-01", "life_expectancy_max": "2028-01-01"},
]
BALANCER = {"active": True, "mode": "upmap", "no_optimization_needed": True, "plans": []}
AUTOSCALE = [
    {"pool_name": ".mgr", "pg_autoscale_mode": "on", "would_adjust": False},
    {"pool_name": "volumes", "pg_autoscale_mode": "on", "would_adjust": False},
]


def _runner(overrides=None, metrics=None, fail=()):
    outputs = {
        "ceph mgr module ls": MODULES, "ceph device ls": DEVICES,
        "ceph balancer status": BALANCER, "ceph osd pool autoscale-status": AUTOSCALE,
        **(overrides or {}),
    }
    calls = []

    def run(command):
        calls.append(command)
        if command in fail:
            raise RuntimeError(f"{command} failed")
        if command.startswith("ceph device get-health-metrics "):
            return (metrics or {}).get(command.rsplit(" ", 1)[1], {})
        return outputs[command]

    run.calls = calls
    return run


def _feature(report, feature_id):
    return next(item for item in report["features"] if item["id"] == feature_id)


def test_module_states_for_list_and_legacy_dict_formats():
    assert cf.parse_mgr_modules(MODULES) == {
        "diskprediction_local": "disabled", "influx": "disabled", "cephadm": "enabled", "dashboard": "enabled",
        "prometheus": "enabled", "balancer": "always_on", "crash": "always_on", "devicehealth": "always_on",
        "orchestrator": "always_on", "pg_autoscaler": "always_on", "status": "always_on",
    }
    legacy = {"always_on_modules": {"octopus": ["balancer", "devicehealth"]}, "enabled_modules": ["iostat"],
              "disabled_modules": []}
    assert cf.parse_mgr_modules(legacy) == {"balancer": "always_on", "devicehealth": "always_on", "iostat": "enabled"}
    assert cf.parse_mgr_modules("garbage") == {}


def test_virtual_disks_without_smart_do_not_recommend_disk_prediction():
    run = _runner()
    report = cf.collect(run)
    prediction = _feature(report, "diskprediction_local")
    assert prediction["state"] == cf.INFO
    assert "Không cần bật" in prediction["recommendation"]
    assert report["devices"]["total"] == 3
    assert report["devices"]["with_prediction"] == 1
    assert report["devices"]["with_smart"] == 0
    assert _feature(report, "devicehealth")["state"] == cf.OK
    assert _feature(report, "balancer")["state"] == cf.OK
    assert _feature(report, "pg_autoscaler")["state"] == cf.OK
    assert report["read_only"] is True and report["errors"] == {}


def test_osd_devices_are_sampled_first_for_smart():
    run = _runner()
    cf.collect(run)
    sampled = [call.rsplit(" ", 1)[1] for call in run.calls if "get-health-metrics" in call]
    assert sampled[:2] == ["osd-disk-b", "osd-disk-c"]
    assert len(sampled) <= cf.HEALTH_METRICS_SAMPLE


SMARTCTL_FAILED = {"20260924-000634": {
    "dev": "/dev/vdb", "error": "smartctl failed", "nvme_vendor": "unknown", "smartctl_error_code": -22,
    "smartctl_output": "smartctl returned an error (137): stderr:\nsudo: exit status: 137\nstdout:\n",
}}


def test_stored_smartctl_failures_are_not_mistaken_for_smart_data():
    # devicehealth keeps failed scrapes; CS-LAB returned these for virtio disks.
    report = cf.collect(_runner(metrics={"osd-disk-b": SMARTCTL_FAILED, "osd-disk-c": SMARTCTL_FAILED}))
    prediction = _feature(report, "diskprediction_local")
    assert prediction["state"] == cf.INFO
    assert "smartctl lỗi trên 2 device" in prediction["summary"]
    assert report["devices"]["with_smart"] == 0 and report["devices"]["smartctl_failed"] == 2
    assert cf.smart_state({}) == "none"
    assert cf.smart_state(SMARTCTL_FAILED) == "failed"


def test_smart_data_with_prediction_off_recommends_enabling_it():
    report = cf.collect(_runner(metrics={"osd-disk-b": {"2026-09-28": {"smart_status": {"passed": True}}}}))
    prediction = _feature(report, "diskprediction_local")
    assert prediction["state"] == cf.WARN
    assert "ceph mgr module enable diskprediction_local" in prediction["recommendation"]


def test_autoscaler_and_balancer_recommendations():
    autoscale = [
        {"pool_name": "rbd", "pg_autoscale_mode": "warn", "would_adjust": True},
        {"pool_name": "images", "pg_autoscale_mode": "on", "would_adjust": True},
    ]
    report = cf.collect(_runner({"ceph osd pool autoscale-status": autoscale,
                                 "ceph balancer status": {"active": False, "mode": "none"}}))
    autoscaler = _feature(report, "pg_autoscaler")
    assert autoscaler["state"] == cf.WARN and "rbd" in autoscaler["summary"]
    assert _feature(report, "balancer")["state"] == cf.WARN
    assert "ceph balancer on" in _feature(report, "balancer")["recommendation"]

    only_pending = cf.collect(_runner({"ceph osd pool autoscale-status": autoscale[1:]}))
    assert _feature(only_pending, "pg_autoscaler")["state"] == cf.INFO


def test_one_failing_command_is_reported_without_hiding_the_others():
    report = cf.collect(_runner(fail=("ceph balancer status",)))
    assert "balancer" in report["errors"]
    assert _feature(report, "balancer")["state"] == cf.UNAVAILABLE
    assert _feature(report, "devicehealth")["state"] == cf.OK

    no_modules = cf.collect(_runner(fail=("ceph mgr module ls",)))
    assert no_modules["features"] == [] and "mgr_modules" in no_modules["errors"]


@pytest.mark.parametrize("devid", ["x; ceph osd out 0", "$(reboot)", "a b", "", "x" * 200])
def test_unsafe_device_ids_are_never_sent_to_the_cluster(devid):
    run = _runner({"ceph device ls": [{"devid": devid, "daemons": ["osd.1"]}]})
    cf.collect(run)
    assert not any("get-health-metrics" in call for call in run.calls)


def test_only_read_only_commands_are_ever_run():
    run = _runner()
    cf.collect(run)
    allowed = set(cf.FEATURE_COMMANDS.values())
    for call in run.calls:
        assert call in allowed or call.startswith("ceph device get-health-metrics ")
    for command in allowed:
        for verb in (" enable", " disable", " on", " off", " set", " rm", " optimize", " execute"):
            assert not command.endswith(verb) and f"{verb} " not in command


def test_api_caches_per_cluster_and_refresh_forces_a_new_read(monkeypatch):
    from dashboard.routes import disk_risk

    class Cluster:
        id = "c1"

    calls = []
    monkeypatch.setattr(disk_risk, "_collect_ceph_features", lambda cluster: calls.append(cluster.id) or {"n": len(calls)})
    disk_risk._features_cache.clear()
    assert disk_risk._cached_ceph_features(Cluster()) == {"n": 1}
    assert disk_risk._cached_ceph_features(Cluster()) == {"n": 1}
    assert disk_risk._cached_ceph_features(Cluster(), refresh=True) == {"n": 2}
    assert calls == ["c1", "c1"]
    disk_risk._features_cache.clear()
