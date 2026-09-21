from shared.bandit_sandbox import VerifiedActionOutcome, run_offline_sandbox


def _outcome(action, reward, verified=True):
    return VerifiedActionOutcome("cluster-a", "node-1", "cpu", action, reward, verified)


def test_bandit_sandbox_uses_only_verified_safe_actions_and_never_executes():
    report = run_offline_sandbox([
        _outcome("OBSERVE", 0.5),
        _outcome("OPEN_TICKET", 0.9),
        _outcome("RESTART_OSD", 100.0),
        _outcome("OPEN_TICKET", 1.0, verified=False),
    ])
    assert report.processed == 2
    assert report.skipped_unverified == 2
    assert report.recommendations[0].action == "OPEN_TICKET"
    assert report.recommendations[0].executable is False


def test_bandit_sandbox_kill_switch_and_budget_are_fail_closed():
    outcomes = [_outcome("OBSERVE", 1.0) for _ in range(10)]
    assert run_offline_sandbox(outcomes, kill_switch=True).stopped_reason == "kill_switch"
    assert run_offline_sandbox(outcomes, max_samples=2).stopped_reason == "sample_budget_exhausted"
