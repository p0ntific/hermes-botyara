import asyncio
import datetime
from zoneinfo import ZoneInfo


def is_outreach_allowed(settings, now=None):
    timezone = ZoneInfo(settings.outreach_timezone)
    local_now = now.astimezone(timezone) if now else datetime.datetime.now(timezone)
    hour = local_now.hour
    start = settings.outreach_quiet_start_hour
    end = settings.outreach_quiet_end_hour
    if start == end:
        return True
    if start < end:
        quiet = start <= hour < end
    else:
        quiet = hour >= start or hour < end
    return not quiet


def seconds_until_outreach(settings, now=None):
    timezone = ZoneInfo(settings.outreach_timezone)
    local_now = now.astimezone(timezone) if now else datetime.datetime.now(timezone)
    if is_outreach_allowed(settings, local_now):
        return 0
    end = local_now.replace(
        hour=settings.outreach_quiet_end_hour,
        minute=0,
        second=0,
        microsecond=0,
    )
    if end <= local_now:
        end += datetime.timedelta(days=1)
    return max(1, int((end - local_now).total_seconds()))


async def wait_for_outreach_window(settings):
    while True:
        delay = seconds_until_outreach(settings)
        if delay == 0:
            return
        await asyncio.sleep(delay)
