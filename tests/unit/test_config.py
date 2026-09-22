"""
Unit tests for Module 1: Configuration Engine.

Run with:
    pytest tests/unit/test_config.py -v
"""


import pytest
from pydantic import ValidationError

from config.loader import load_config
from config.schema import ConfidenceConfig, PlatformConfig, RiskConfig, StrategyProfileName


def test_loads_successfully_with_real_project_files():
    """The real defaults.yaml + balanced.yaml must produce a valid config."""
    config = load_config()
    assert isinstance(config, PlatformConfig)
    assert config.metadata.strategy_profile == StrategyProfileName.BALANCED


def test_model_defaults_alone_produce_a_valid_config(tmp_path):
    """
    Even with a missing/empty defaults.yaml, the platform must still be able
    to start (SRS: 'Never crash because of missing settings'). A profile
    file is still required to exist, because an *unresolvable* profile name
    is a genuine configuration error, not an absent optional file.
    """
    empty_defaults = tmp_path / "does_not_exist.yaml"
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir()
    (profiles_dir / "balanced.yaml").write_text("metadata:\n  strategy_profile: balanced\n")

    config = load_config(defaults_path=empty_defaults, profiles_dir=profiles_dir)
    assert isinstance(config, PlatformConfig)
    assert config.risk.min_risk_reward == 2.0  # pure Pydantic model default
    assert config.confidence.institutional_grade == 95.0


def test_unknown_strategy_profile_raises(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", "does_not_exist")
    with pytest.raises(FileNotFoundError):
        load_config()


def test_negative_risk_reward_is_rejected():
    with pytest.raises(ValidationError):
        RiskConfig(min_risk_reward=-1.0)


def test_risk_reward_bands_out_of_order_is_rejected():
    with pytest.raises(ValidationError):
        RiskConfig(min_risk_reward=3.0, good_risk_reward=2.5, excellent_risk_reward=2.0)


def test_dynamic_sl_tp_cannot_be_disabled():
    """SRS Rule 5 (NO FIXED TP OR SL) is non-negotiable, under any profile."""
    with pytest.raises(ValidationError):
        RiskConfig(dynamic_sl_enabled=False)
    with pytest.raises(ValidationError):
        RiskConfig(dynamic_tp_enabled=False)


def test_out_of_order_confidence_bands_are_rejected():
    with pytest.raises(ValidationError):
        ConfidenceConfig(
            minimum_confidence=80,
            strong_grade=80,
            very_strong_grade=70,  # lower than strong_grade -> invalid
            excellent_grade=90,
            institutional_grade=95,
        )


def test_minimum_confidence_cannot_exceed_strong_grade():
    with pytest.raises(ValidationError):
        ConfidenceConfig(minimum_confidence=90, strong_grade=80)


def test_confidence_grade_for_score_helper():
    cfg = ConfidenceConfig()
    assert cfg.grade_for(96) == "INSTITUTIONAL_GRADE"
    assert cfg.grade_for(91) == "EXCELLENT"
    assert cfg.grade_for(87) == "VERY_STRONG"
    assert cfg.grade_for(82) == "STRONG"
    assert cfg.grade_for(50) == "REJECTED"


def test_invalid_timezone_is_rejected():
    from config.schema import GeneralConfig

    with pytest.raises(ValidationError):
        GeneralConfig(timezone="Not/ARealZone")


def test_env_var_overrides_take_precedence(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "test-token-123")
    monkeypatch.setenv("DATABASE_PATH", "/tmp/test_platform.db")
    config = load_config()
    assert config.telegram.bot_token == "test-token-123"
    assert config.database.path == "/tmp/test_platform.db"


def test_numeric_env_var_overrides_are_coerced_to_the_right_type(monkeypatch):
    """
    Regression test: these six env vars used to be accepted by Railway
    but silently ignored (not in _ENV_OVERRIDES at all -- see
    config/loader.py's module docstring). Also verifies Pydantic actually
    coerces the raw string env value to the field's real int/float type,
    not just that the value round-trips as a string.
    """
    monkeypatch.setenv("MIN_CONFIDENCE", "72.5")
    monkeypatch.setenv("MIN_QUOTE_VOLUME_USDT", "8000000")
    monkeypatch.setenv("SCAN_INTERVAL_SECONDS", "45")
    monkeypatch.setenv("MAX_SYMBOLS_TO_ANALYZE", "120")
    monkeypatch.setenv("SIGNAL_COOLDOWN_MINUTES", "30")
    monkeypatch.setenv("BINANCE_TIMEOUT_SECONDS", "15.5")

    config = load_config()

    assert config.confidence.minimum_confidence == 72.5
    assert isinstance(config.confidence.minimum_confidence, float)
    assert config.scanner.min_24h_quote_volume_usdt == 8_000_000
    assert config.scanner.fast_scan_interval_seconds == 45
    assert isinstance(config.scanner.fast_scan_interval_seconds, int)
    assert config.scanner.max_symbols_to_analyze == 120
    assert config.risk.signal_cooldown_minutes == 30
    assert config.api.request_timeout_seconds == 15.5
    assert isinstance(config.api.request_timeout_seconds, float)


def test_max_symbols_to_analyze_and_signal_cooldown_default_to_no_cap(monkeypatch):
    """Unset -> None -> today's existing behavior (no cap / no cooldown), not silently a real number."""
    config = load_config()
    assert config.scanner.max_symbols_to_analyze is None
    assert config.risk.signal_cooldown_minutes is None


def test_non_numeric_value_for_a_numeric_env_var_fails_loudly(monkeypatch):
    """Same "fail loud on genuinely wrong config" behavior RUN_MODE already relies on -- never silently ignored, never silently zero."""
    monkeypatch.setenv("MAX_SYMBOLS_TO_ANALYZE", "not-a-number")
    with pytest.raises(ValidationError):
        load_config()


def test_run_mode_env_override_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("RUN_MODE", "LIVE")
    config = load_config()
    assert config.general.run_mode.value == "live"


def test_strategy_profiles_produce_different_risk_settings(monkeypatch):
    monkeypatch.setenv("STRATEGY_PROFILE", "conservative")
    conservative = load_config()

    monkeypatch.setenv("STRATEGY_PROFILE", "aggressive")
    aggressive = load_config()

    monkeypatch.setenv("STRATEGY_PROFILE", "professional")
    professional = load_config()

    assert conservative.confidence.minimum_confidence > aggressive.confidence.minimum_confidence
    assert conservative.risk.max_active_trades < aggressive.risk.max_active_trades
    assert professional.confidence.minimum_confidence > conservative.confidence.minimum_confidence
    assert professional.risk.max_active_trades <= conservative.risk.max_active_trades

    # Every profile must still respect Rule 5 (dynamic TP/SL mandatory)
    for cfg in (conservative, aggressive, professional):
        assert cfg.risk.dynamic_sl_enabled is True
        assert cfg.risk.dynamic_tp_enabled is True


def test_secrets_are_masked_in_safe_dict(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "super-secret-token")
    monkeypatch.setenv("BINANCE_API_SECRET", "super-secret-api-key")
    config = load_config()

    safe = config.safe_dict()

    assert safe["telegram"]["bot_token"] == "***MASKED***"
    assert safe["api"]["binance_api_secret"] == "***MASKED***"
    assert "super-secret-token" not in str(safe)
    assert "super-secret-api-key" not in str(safe)


def test_get_config_singleton_returns_same_instance():
    from config.loader import get_config

    first = get_config()
    second = get_config()
    assert first is second

    third = get_config(force_reload=True)
    assert isinstance(third, PlatformConfig)
