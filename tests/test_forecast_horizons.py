from shared.forecast_horizons import parse_horizons


def test_parse_horizons_returns_supported_values_in_order():
    assert parse_horizons("24,1,6,6") == (1, 6, 24)


def test_parse_horizons_ignores_unsupported_values_without_silent_fallback():
    assert parse_horizons("1,12,invalid") == (1,)


def test_parse_horizons_uses_product_default_when_config_is_empty_or_invalid():
    assert parse_horizons("") == (1, 6, 24)
    assert parse_horizons("12,48") == (1, 6, 24)
