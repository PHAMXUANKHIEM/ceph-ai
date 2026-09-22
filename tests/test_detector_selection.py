from shared.detector_selection import DetectorWindowEvidence, choose_detector


def _evidence(detector, index, *, fpr, delay, unit="samples", cpu=10):
    return DetectorWindowEvidence(f"window-{index}", fpr, delay, unit, cpu)


def test_detector_selection_requires_aligned_acceptance_windows():
    baseline = [_evidence("river", index, fpr=.1, delay=10) for index in range(3)]
    candidate = [_evidence("alibi", index, fpr=.05, delay=5) for index in range(3)]
    decision = choose_detector(baseline, candidate)
    assert decision.status == "SELECTED"
    assert decision.selected == "candidate"


def test_detector_selection_holds_for_unit_mismatch_or_regression():
    baseline = [_evidence("river", index, fpr=.1, delay=10) for index in range(3)]
    mismatched = [_evidence("alibi", index, fpr=.05, delay=1, unit="chunks") for index in range(3)]
    assert "delay_unit" in choose_detector(baseline, mismatched).failed_checks
    worse = [_evidence("alibi", index, fpr=.2, delay=5) for index in range(3)]
    assert "false_positive_rate" in choose_detector(baseline, worse).failed_checks
