"""
core/routines/quiet_hours.py — Quiet hours window configuration and speech suppression rules.

Rule:
- Suppresses unprompted speech during quiet hours window (e.g., 22:00 - 07:00).
- Exact-time reminders (is_exact_time=True) STILL FIRE during quiet hours.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time as dtime
import json
from typing import Optional

from memory.config_manager import load_api_keys, save_api_keys

_CONFIG_KEY = "quiet_hours"


@dataclass
class QuietHoursConfig:
    enabled: bool = True
    start_time: str = "22:00"  # 10:00 PM
    end_time: str = "07:00"  # 07:00 AM

    def parse_time(self, t_str: str) -> dtime:
        parts = t_str.strip().split(":")
        h = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 0
        return dtime(hour=h, minute=m)


def get_quiet_hours_config() -> QuietHoursConfig:
    from memory.config_manager import CONFIG_FILE

    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    cfg = data.get(_CONFIG_KEY, {})
    return QuietHoursConfig(
        enabled=bool(cfg.get("enabled", True)),
        start_time=str(cfg.get("start_time", "22:00")),
        end_time=str(cfg.get("end_time", "07:00")),
    )


def save_quiet_hours_config(enabled: bool, start_time: str = "22:00", end_time: str = "07:00") -> None:
    from memory.config_manager import CONFIG_FILE, ensure_config_dir

    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data[_CONFIG_KEY] = {
        "enabled": bool(enabled),
        "start_time": str(start_time),
        "end_time": str(end_time),
    }
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def is_in_quiet_hours(now: Optional[datetime] = None, config: Optional[QuietHoursConfig] = None) -> bool:
    """Check if the current wall-clock time falls inside the configured quiet hours."""
    cfg = config or get_quiet_hours_config()
    if not cfg.enabled:
        return False

    current_dt = now or datetime.now()
    cur_t = current_dt.time()
    st = cfg.parse_time(cfg.start_time)
    et = cfg.parse_time(cfg.end_time)

    if st < et:
        # Same-day range, e.g. 13:00 to 15:00
        return st <= cur_t < et
    else:
        # Overnight range, e.g. 22:00 to 07:00
        return cur_t >= st or cur_t < et


def should_suppress_speech(
    is_exact_time: bool = False, now: Optional[datetime] = None, config: Optional[QuietHoursConfig] = None
) -> bool:
    """
    Decide whether speech should be suppressed.
    Exact-time reminders are exempted and still fire.
    """
    if is_exact_time:
        return False
    return is_in_quiet_hours(now, config)
