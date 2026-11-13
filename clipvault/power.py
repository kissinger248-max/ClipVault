"""Laptop power awareness: heavy steps wait for mains power (SPEC §4)."""

from __future__ import annotations

import logging

from clipvault.config import Config

log = logging.getLogger(__name__)


def on_battery() -> bool:
    """True when running on battery. Desktops, servers and unknown cases count as mains."""
    try:
        import psutil

        battery = psutil.sensors_battery()
    except Exception:  # no sensor support counts as "plugged in"
        return False
    return battery is not None and battery.power_plugged is False


def heavy_allowed(config: Config) -> bool:
    """May transcription, rendering and VOD cuts run right now?"""
    return config.heavy_jobs_on_battery or not on_battery()
