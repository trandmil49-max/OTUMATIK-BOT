"""
Configuration Engine (SRS Part 19).

Public API:
    get_config()   -> the process-wide validated PlatformConfig singleton
    load_config()  -> build a fresh PlatformConfig (rarely needed directly;
                       mainly used by tests and by get_config(force_reload=True))
    PlatformConfig -> the root config model, for type hints elsewhere in the codebase
"""

from config.loader import get_config, load_config
from config.schema import PlatformConfig

__all__ = ["get_config", "load_config", "PlatformConfig"]
