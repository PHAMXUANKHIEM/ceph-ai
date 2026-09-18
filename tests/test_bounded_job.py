from worker.executor import bounded_job


def test_bounded_job_uses_executor_mock_in_tests(monkeypatch):
    calls = []

    class Executor:
        @staticmethod
        def run(*args):
            calls.append(args)
            return True

    monkeypatch.setenv("PYTEST_CURRENT_TEST", "resource-test")
    result = bounded_job.run_bounded_benchmark(
        "action-1",
        {"pool": "rbd"},
        "incident-1",
        lambda *_: None,
        executor_module=Executor,
        executor_name="volume",
    )

    assert result is True
    assert calls[0][:3] == ("action-1", {"pool": "rbd"}, "incident-1")


def test_child_limit_policy_is_one_cpu_and_two_gib(monkeypatch):
    affinity_calls = []
    rlimit_calls = []
    monkeypatch.setattr(bounded_job.os, "sched_getaffinity", lambda _: {2, 3})
    monkeypatch.setattr(
        bounded_job.os,
        "sched_setaffinity",
        lambda pid, cpus: affinity_calls.append((pid, cpus)),
    )
    monkeypatch.setattr(
        bounded_job.resource,
        "getrlimit",
        lambda _: (bounded_job.resource.RLIM_INFINITY, bounded_job.resource.RLIM_INFINITY),
    )
    monkeypatch.setattr(
        bounded_job.resource,
        "setrlimit",
        lambda resource_id, limits: rlimit_calls.append((resource_id, limits)),
    )
    bounded_job._apply_child_limits()
    assert affinity_calls == [(0, {2})]
    assert rlimit_calls == [
        (bounded_job.resource.RLIMIT_AS, (2 * 1024 * 1024 * 1024,) * 2)
    ]
