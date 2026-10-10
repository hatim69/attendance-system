from datetime import datetime
from zoneinfo import ZoneInfo

from . import config


def now_local() -> datetime:
    """Current wall-clock time in the workplace timezone, naive (same form the devices report)."""
    return datetime.now(ZoneInfo(config.TIMEZONE)).replace(tzinfo=None, microsecond=0)
