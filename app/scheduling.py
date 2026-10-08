"""Validate reviewed send times independently of the Windows time zone."""

from datetime import datetime, timedelta, timezone
import re
from zoneinfo import ZoneInfo


TIME_ZONE = "Europe/Warsaw"
MAX_LATENESS_SECONDS = 15 * 60


def parse_schedule(schedule, reference_time=None):
    if schedule is None:
        return None
    if not isinstance(schedule, dict):
        raise ValueError("Choose a schedule type and send time.")
    reference_time = reference_time or datetime.now(timezone.utc)
    mode = schedule.get("mode")
    if mode == "delay":
        minutes = schedule.get("minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, int) or not 1 <= minutes <= 525600:
            raise ValueError("Choose a delay from 1 to 525,600 minutes.")
        target = reference_time + timedelta(minutes=minutes)
    elif mode == "datetime":
        if schedule.get("time_zone", TIME_ZONE) != TIME_ZONE:
            raise ValueError(f"Scheduled dates use {TIME_ZONE}.")
        value = schedule.get("local_time")
        if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?", value):
            raise ValueError("Choose a valid send date and time.")
        local_time = datetime.fromisoformat(value)
        zone = ZoneInfo(TIME_ZONE)
        candidates = set()
        for fold in (0, 1):
            candidate = local_time.replace(tzinfo=zone, fold=fold).astimezone(timezone.utc)
            if candidate.astimezone(zone).replace(tzinfo=None) == local_time:
                candidates.add(candidate)
        if len(candidates) != 1:
            raise ValueError("This time is skipped or repeated by daylight saving. Choose another time or use a delay in minutes.")
        target = candidates.pop()
    else:
        raise ValueError("Choose a date and time or a delay in minutes.")
    if not reference_time < target <= reference_time + timedelta(days=365):
        raise ValueError("Choose a future send time within the next 365 days.")
    return {"scheduled_at": target.astimezone(timezone.utc).isoformat(timespec="seconds"), "time_zone": TIME_ZONE}
