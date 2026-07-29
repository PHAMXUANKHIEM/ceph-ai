import json
from types import SimpleNamespace

import pytest

import dashboard.volume_perf_analysis as volume_perf_analysis
from dashboard.volume_perf_analysis import VolumePerfAnalysisError, analyze_volume_perf_sweep
from shared.router_client import RouterNotConfiguredError

_SWEEP = {
    "pool": "vms",
    "scratch_image": "_ceph_aiops_perf_probe",
    "steps": [
        {"iodepth": 1, "iops": 1000, "latency_avg_ms": 1.0, "latency_p99_ms": 1.05},
        {"iodepth": 16, "iops": 16000, "latency_avg_ms": 1.0, "latency_p99_ms": 1.8},
        {"iodepth": 32, "iops": 16500, "latency_avg_ms": 1.9, "latency_p99_ms": 30.0},
    ],
    "knee": {"iodepth": 16, "iops": 16000, "latency_avg_ms": 1.0, "latency_p99_ms": 1.8},
    "qos_notes": "Không có giới hạn QoS nào được đặt trên scratch image.",
    "bottleneck_notes": "ceph osd perf:\nosd.0 1 2",
}

_CONCLUSION = {
    "max_iops": 16000,
    "max_iops_basis": "saturation_knee",
    "confidence": "high",
    "conclusion_vi": "Hiệu năng tối đa khoảng 16000 IOPS.",
    "caveats_vi": "Nút thắt có thể nằm ở OSD.",
}


class _FakeToolCall:
    def __init__(self, name: str, args: dict):
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args))


class _FakeStream:
    def __init__(self, completion):
        self._completion = completion

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_final_completion(self):
        return self._completion


def _completion(*calls, finish_reason="stop"):
    tool_calls = [_FakeToolCall(name, args) for name, args in calls]
    message = SimpleNamespace(tool_calls=tool_calls, content=None)
    choice = SimpleNamespace(message=message, finish_reason=finish_reason)
    return SimpleNamespace(choices=[choice])


def _install_fake_client(monkeypatch, completion):
    class FakeCompletions:
        def stream(self, **kwargs):
            return _FakeStream(completion)

    class FakeChat:
        completions = FakeCompletions()

    class FakeClient:
        chat = FakeChat()

    monkeypatch.setattr(volume_perf_analysis, "_get_client", lambda: FakeClient())


def test_analyze_returns_parsed_conclusion(monkeypatch):
    completion = _completion((volume_perf_analysis.TOOL_NAME, _CONCLUSION))
    _install_fake_client(monkeypatch, completion)

    result = _run(analyze_volume_perf_sweep(_SWEEP))

    assert result == _CONCLUSION


def test_analyze_raises_when_router_not_configured(monkeypatch):
    def broken():
        raise RouterNotConfiguredError("Chưa cấu hình API AI")

    monkeypatch.setattr(volume_perf_analysis, "_get_client", broken)

    with pytest.raises(VolumePerfAnalysisError):
        _run(analyze_volume_perf_sweep(_SWEEP))


def test_analyze_raises_when_no_steps():
    with pytest.raises(VolumePerfAnalysisError):
        _run(analyze_volume_perf_sweep({"pool": "vms", "steps": []}))


def test_analyze_raises_on_truncated_response(monkeypatch):
    completion = _completion((volume_perf_analysis.TOOL_NAME, _CONCLUSION), finish_reason="length")
    _install_fake_client(monkeypatch, completion)

    with pytest.raises(VolumePerfAnalysisError):
        _run(analyze_volume_perf_sweep(_SWEEP))


def test_analyze_raises_when_no_tool_call_made(monkeypatch):
    completion = _completion(finish_reason="stop")
    _install_fake_client(monkeypatch, completion)

    with pytest.raises(VolumePerfAnalysisError):
        _run(analyze_volume_perf_sweep(_SWEEP))


def test_analyze_raises_when_required_field_missing(monkeypatch):
    incomplete = {k: v for k, v in _CONCLUSION.items() if k != "confidence"}
    completion = _completion((volume_perf_analysis.TOOL_NAME, incomplete))
    _install_fake_client(monkeypatch, completion)

    with pytest.raises(VolumePerfAnalysisError):
        _run(analyze_volume_perf_sweep(_SWEEP))


def _run(coro):
    import asyncio

    return asyncio.run(coro)
