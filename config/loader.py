"""
config/loader.py

Builds the validated PlatformConfig by merging, in this exact order
(later layers win):

    1. Pydantic model field defaults   (hardcoded safe fallbacks — see schema.py)
    2. config/defaults.yaml            (human-editable baseline)
    3. config/profiles/<profile>.yaml  (strategy-specific overlay)
    4. Environment variables           (secrets & deployment overrides)

This order is deliberate:
  * Layer 1 guarantees the platform can start even with zero external files
    (SRS Part 19: "If configuration is missing, load safe defaults. Never
    crash because of missing settings.").
  * Layer 3 is how "Profile Switching" works: SRS Part 19 requires that
    "changing profile must NEVER require code modifications. One
    configuration change should be enough." -> a single STRATEGY_PROFILE
    value selects the whole overlay.
  * Layer 4 is intentionally last and explicit (not "magic" nested-env
    parsing) so secret handling stays auditable: every environment
    variable this loader reads is listed in _ENV_OVERRIDES below, and
    nowhere else.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Optional

import yaml

from config.schema import PlatformConfig, StrategyProfileName

BASE_DIR = Path(__file__).resolve().parent
DEFAULTS_PATH = BASE_DIR / "defaults.yaml"
PROFILES_DIR = BASE_DIR / "profiles"

# Explicit env-var -> config-path mapping. Explicit beats implicit for a
# system where a silently-wrong Telegram token or DB path is a real
# production problem, not a cosmetic bug.
#
# Numeric values (MIN_CONFIDENCE, MIN_QUOTE_VOLUME_USDT,
# MAX_SYMBOLS_TO_ANALYZE, SCAN_INTERVAL_SECONDS,
# SIGNAL_COOLDOWN_MINUTES, BINANCE_TIMEOUT_SECONDS) are inserted as raw strings here, same as
# every other entry -- Pydantic's own validation in load_config()'s
# final PlatformConfig.model_validate() call coerces a numeric string to
# the field's real int/float type (confirmed: a non-numeric string
# still fails loudly there, as a normal ValidationError, exactly the
# "fail loud on genuinely wrong config" behavior the rest of this loader
# already relies on for e.g. RUN_MODE).
_ENV_OVERRIDES: dict[str, tuple[str, ...]] = {
    "TELEGRAM_BOT_TOKEN": ("telegram", "bot_token"),
    "TELEGRAM_CHAT_ID": ("telegram", "chat_id"),
    "BINANCE_API_KEY": ("api", "binance_api_key"),
    "BINANCE_API_SECRET": ("api", "binance_api_secret"),
    "DATABASE_PATH": ("database", "path"),
    "RUN_MODE": ("general", "run_mode"),
    "STRATEGY_PROFILE": ("metadata", "strategy_profile"),
    "LOG_LEVEL": ("logging", "level"),
    "DEBUG_MODE": ("general", "debug_mode"),
    "MIN_CONFIDENCE": ("confidence", "minimum_confidence"),
    "MIN_QUOTE_VOLUME_USDT": ("scanner", "min_24h_quote_volume_usdt"),
    "SCAN_INTERVAL_SECONDS": ("scanner", "fast_scan_interval_seconds"),
    "MAX_SYMBOLS_TO_ANALYZE": ("scanner", "max_symbols_to_analyze"),
    "SIGNAL_COOLDOWN_MINUTES": ("risk", "signal_cooldown_minutes"),
    "BINANCE_TIMEOUT_SECONDS": ("api", "request_timeout_seconds"),
    "BINANCE_BASE_URL": ("api", "binance_base_url"),
    "TRADING_ENABLED": ("api", "trading_enabled"),
    "BINGX_API_KEY": ("api", "bingx_api_key"),
    "BINGX_API_SECRET": ("api", "bingx_api_secret"),
    "BINGX_BASE_URL": ("api", "bingx_base_url"),
}

_BOOL_ENV_KEYS = {"DEBUG_MODE", "TRADING_ENABLED"}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` into `base`; returns a new dict, never mutates inputs."""
    result = copy.deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml(path: Path) -> dict[str, Any]:
    """Missing files are treated as empty overlays, never as errors."""
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file {path} must contain a YAML mapping at the top level")
    return data


def _coerce_bool(raw: str) -> bool:
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _apply_env_overrides(data: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(data)
    for env_name, path in _ENV_OVERRIDES.items():
        raw_value = os.environ.get(env_name)
        if raw_value is None:
            continue
        value: Any = _coerce_bool(raw_value) if env_name in _BOOL_ENV_KEYS else raw_value
        node = result
        for key in path[:-1]:
            node = node.setdefault(key, {})
        node[path[-1]] = value
    return result


def _resolve_profile_name(merged_yaml: dict[str, Any]) -> str:
    """STRATEGY_PROFILE env var wins over whatever defaults.yaml declares."""
    env_profile = os.environ.get("STRATEGY_PROFILE")
    if env_profile:
        return env_profile.strip().lower()
    return (
        merged_yaml.get("metadata", {}).get("strategy_profile")
        or StrategyProfileName.BALANCED.value
    )


def load_config(
    defaults_path: Path = DEFAULTS_PATH,
    profiles_dir: Path = PROFILES_DIR,
) -> PlatformConfig:
    """
    Build a fresh, validated PlatformConfig from the four-layer merge
    described in this module's docstring.

    Raises:
        FileNotFoundError: the resolved strategy profile has no matching
            YAML file under `profiles_dir` (a genuinely wrong profile name
            should fail loudly, unlike a missing *optional* file).
        pydantic.ValidationError: any merged value fails schema validation
            (out-of-range number, badly-ordered thresholds, etc.).
    """
    merged: dict[str, Any] = {}

    defaults_data = _load_yaml(defaults_path)
    merged = _deep_merge(merged, defaults_data)

    profile_name = _resolve_profile_name(merged)
    profile_path = profiles_dir / f"{profile_name}.yaml"
    if not profile_path.exists():
        valid = [p.value for p in StrategyProfileName]
        raise FileNotFoundError(
            f"Unknown strategy profile '{profile_name}': expected file {profile_path}. "
            f"Valid profiles: {valid}"
        )
    profile_data = _load_yaml(profile_path)
    merged = _deep_merge(merged, profile_data)
    merged.setdefault("metadata", {})["strategy_profile"] = profile_name

    merged = _apply_env_overrides(merged)

    return PlatformConfig.model_validate(merged)


_config_singleton: Optional[PlatformConfig] = None


def get_config(force_reload: bool = False) -> PlatformConfig:
    """
    Return the process-wide validated config singleton, loading it on first
    use. All engine modules should call this rather than constructing their
    own PlatformConfig, so the entire process shares one validated instance.
    """
    global _config_singleton
    if _config_singleton is None or force_reload:
        _config_singleton = load_config()
    return _config_singleton
