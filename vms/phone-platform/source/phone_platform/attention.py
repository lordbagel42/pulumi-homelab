"""Durable callback scheduling and local-time quiet hours."""
from datetime import datetime
from zoneinfo import ZoneInfo

DEFAULT_QUIET = {"enabled": False, "start": "22:00", "end": "08:00", "timezone": "America/Boise"}


def is_quiet(settings, now=None):
    if not settings.get("enabled"):
        return False
    now = (now or datetime.now(ZoneInfo("UTC"))).astimezone(ZoneInfo(settings["timezone"]))
    minute = now.hour * 60 + now.minute
    def minutes(value):
        hour, minute = map(int, value.split(":"))
        return hour * 60 + minute
    start, end = minutes(settings["start"]), minutes(settings["end"])
    return start <= minute < end if start < end else minute >= start or minute < end
