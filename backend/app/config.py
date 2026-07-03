"""Backend settings: shared config comes from jobpilot_core; app metadata here."""

from __future__ import annotations

from jobpilot_core.settings import Settings, get_settings

APP_NAME = "jobpilot-backend"
VERSION = "0.1.0"

__all__ = ["Settings", "get_settings", "APP_NAME", "VERSION"]
