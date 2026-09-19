from types import SimpleNamespace

import shared.ai_routing as ai_routing


def _price(provider, model, input_rate, output_rate):
    return SimpleNamespace(
        provider=provider,
        model_id=model,
        input_usd_per_million_tokens=input_rate,
        output_usd_per_million_tokens=output_rate,
    )


def test_advisory_mode_only_reports_cheaper_candidate(monkeypatch):
    current = _price("9router", "expensive", 10, 20)
    cheaper = _price("9router", "cheap", 1, 2)
    monkeypatch.setattr(ai_routing, "_resolve_price", lambda *_: current)
    monkeypatch.setattr(ai_routing, "_active_prices", lambda: (current, cheaper))
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_mode", "advisory")
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_features", "ceph_chat")

    result = ai_routing.choose_model(
        "ceph_chat", "9router", "expensive", input_chars=4000, output_tokens=1000,
    )

    assert result["recommended_model"] == "cheap"
    assert result["selected_model"] == "expensive"
    assert result["canary"] is False


def test_canary_mode_selects_only_inside_configured_percentage(monkeypatch):
    current = _price("9router", "expensive", 10, 20)
    cheaper = _price("9router", "cheap", 1, 2)
    monkeypatch.setattr(ai_routing, "_resolve_price", lambda *_: current)
    monkeypatch.setattr(ai_routing, "_active_prices", lambda: (current, cheaper))
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_mode", "canary")
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_features", "ceph_chat")
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_canary_percent", 100)
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_min_savings_percent", 1)
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_model_allowlist", "9router:cheap")

    result = ai_routing.choose_model(
        "ceph_chat", "9router", "expensive", input_chars=4000, output_tokens=1000,
        request_id="test-request",
    )

    assert result["selected_model"] == "cheap"
    assert result["canary"] is True


def test_routing_does_not_cross_provider_by_default(monkeypatch):
    current = _price("9router", "expensive", 10, 20)
    cheaper_other_provider = _price("claude", "cheap", 1, 2)
    monkeypatch.setattr(ai_routing, "_resolve_price", lambda *_: current)
    monkeypatch.setattr(ai_routing, "_active_prices", lambda: (current, cheaper_other_provider))
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_mode", "canary")
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_features", "ceph_chat")
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_canary_percent", 100)
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_model_allowlist", "")

    result = ai_routing.choose_model(
        "ceph_chat", "9router", "expensive", input_chars=4000, output_tokens=1000,
    )

    assert result["recommended_model"] is None
    assert result["selected_model"] == "expensive"


def test_canary_never_crosses_provider_even_if_legacy_setting_is_false(monkeypatch):
    current = _price("9router", "expensive", 10, 20)
    cheaper_other_provider = _price("claude", "cheap", 1, 2)
    monkeypatch.setattr(ai_routing, "_resolve_price", lambda *_: current)
    monkeypatch.setattr(ai_routing, "_active_prices", lambda: (current, cheaper_other_provider))
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_mode", "canary")
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_features", "ceph_chat")
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_same_provider_only", False)
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_canary_percent", 100)
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_model_allowlist", "claude:cheap")

    result = ai_routing.choose_model(
        "ceph_chat", "9router", "expensive", input_chars=4000, output_tokens=1000,
    )

    assert result["recommended_model"] is None
    assert result["selected_model"] == "expensive"


def test_canary_requires_verified_allowlist(monkeypatch):
    current = _price("9router", "expensive", 10, 20)
    cheaper = _price("9router", "cheap", 1, 2)
    monkeypatch.setattr(ai_routing, "_resolve_price", lambda *_: current)
    monkeypatch.setattr(ai_routing, "_active_prices", lambda: (current, cheaper))
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_mode", "canary")
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_features", "ceph_chat")
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_canary_percent", 100)
    monkeypatch.setattr(ai_routing.settings, "ai_cost_routing_model_allowlist", "")

    result = ai_routing.choose_model(
        "ceph_chat", "9router", "expensive", input_chars=4000, output_tokens=1000,
    )

    assert result["selected_model"] == "expensive"
    assert "allowlist" in result["reason"]
