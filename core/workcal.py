"""Saudi working calendar for internal deadlines: Sunday to Thursday, 08:00–17:00 Riyadh time, minus public holidays
(Settings → Calendar). Amazon's own deadlines (acknowledge within 24 hours, ship dates) stay on the clock."""
import time as _time
from datetime import datetime, time, timedelta

from django.utils import timezone

WEEKEND = {4, 5}           # Friday, Saturday (Monday = 0)
DAY_START, DAY_END = time(8, 0), time(17, 0)
_cache = {"at": 0.0, "days": frozenset()}


def holidays():
    if _time.monotonic() - _cache["at"] > 60:
        from .models import Holiday
        _cache.update(at=_time.monotonic(), days=frozenset(Holiday.objects.values_list("day", flat=True)))
    return _cache["days"]


def clear_cache():
    _cache["at"] = 0.0


def is_working_day(d):
    return d.weekday() not in WEEKEND and d not in holidays()


def add_working_hours(start, hours):
    """start + the given number of working hours, counting only 08:00–17:00 on working days."""
    t = timezone.localtime(start)
    tz = t.tzinfo
    remaining = timedelta(hours=hours)
    while True:
        d = t.date()
        if not is_working_day(d) or t.time() >= DAY_END:
            nxt = d + timedelta(days=1)
            while not is_working_day(nxt):
                nxt += timedelta(days=1)
            t = datetime.combine(nxt, DAY_START, tz)
            continue
        if t.time() < DAY_START:
            t = datetime.combine(d, DAY_START, tz)
        left_today = datetime.combine(d, DAY_END, tz) - t
        if remaining <= left_today:
            return t + remaining
        remaining -= left_today
        t = datetime.combine(d, DAY_END, tz)


def working_hours_between(a, b):
    """Working hours (08:00–17:00 on working days) from a to b; 0 if b is before a."""
    if not a or not b or b <= a:
        return 0.0
    a, b = timezone.localtime(a), timezone.localtime(b)
    tz, total, d = a.tzinfo, 0.0, a.date()
    while d <= b.date():
        if is_working_day(d):
            lo = max(datetime.combine(d, DAY_START, tz), a)
            hi = min(datetime.combine(d, DAY_END, tz), b)
            if hi > lo:
                total += (hi - lo).total_seconds() / 3600
        d += timedelta(days=1)
    return total


def add_working_days(start, days):
    """The same time of day, `days` working days later."""
    t = timezone.localtime(start)
    d = t.date()
    n = 0
    while n < days:
        d += timedelta(days=1)
        if is_working_day(d):
            n += 1
    return t.replace(year=d.year, month=d.month, day=d.day)


def ensure_fixed_holidays(years=None):
    """Founding Day and National Day for this year and next (used by the demo seed after a reset)."""
    from datetime import date
    from .models import Holiday
    y0 = timezone.localdate().year
    for y in years or (y0, y0 + 1):
        for m, d, name in ((2, 22, "Founding Day"), (9, 23, "Saudi National Day")):
            Holiday.objects.get_or_create(day=date(y, m, d), defaults={"name": name})
    clear_cache()
