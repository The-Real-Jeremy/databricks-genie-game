"""The weekly rotation, as a pure function of the date.

No scheduler, no job, no trigger — a fifth component would break the four-component budget, and a
scheduled rotation cannot be tested before the week it fires. This can: pass `today` and get any week.
"""
import datetime as _dt

try:
    from zoneinfo import ZoneInfo
    _HAVE_TZDB = True
except Exception:                                    # pragma: no cover - stdlib present since 3.9
    ZoneInfo = None
    _HAVE_TZDB = False


def local_today(tz_name="Australia/Sydney", tz_offset_hours=10.0, now=None):
    """Today's date in the season's timezone.

    Prefers the real tz database (handles DST). Falls back to a fixed offset if the container has no
    tzdata — a weekly boundary that moves by an hour twice a year is immaterial, an exception is not.
    Which path was taken is reported by `tz_mode()` so it is never a silent difference.
    """
    now = now or _dt.datetime.now(_dt.timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=_dt.timezone.utc)
    if _HAVE_TZDB:
        try:
            return now.astimezone(ZoneInfo(tz_name)).date()
        except Exception:
            pass
    return now.astimezone(_dt.timezone(_dt.timedelta(hours=tz_offset_hours))).date()


def tz_mode(tz_name="Australia/Sydney"):
    if _HAVE_TZDB:
        try:
            ZoneInfo(tz_name)
            return f"tzdb:{tz_name}"
        except Exception:
            return f"fixed-offset (tzdb has no {tz_name})"
    return "fixed-offset (no tzdata in container)"


def parse_date(s):
    return _dt.date.fromisoformat(s)


def week_index(today, start_date, total_weeks=8):
    """1-based week number. Before the start date -> 1. After the season -> `total_weeks`."""
    delta_days = (today - start_date).days
    if delta_days < 0:
        return 1
    return min(delta_days // 7 + 1, total_weeks)


def week_bounds(week, start_date):
    """(first_day, last_day) of a week, inclusive — used for 'this week' scoreboards."""
    first = start_date + _dt.timedelta(days=(week - 1) * 7)
    return first, first + _dt.timedelta(days=6)


def season_state(today, start_date, total_weeks=8, past_weeks_open=True):
    """Everything the UI needs about time, computed once, server-side."""
    wk = week_index(today, start_date, total_weeks)
    first, last = week_bounds(wk, start_date)
    unlocked = list(range(1, wk + 1)) if past_weeks_open else [wk]
    days_left = (last - today).days
    return {
        "week": wk,
        "total_weeks": total_weeks,
        "week_start": first.isoformat(),
        "week_end": last.isoformat(),
        "today": today.isoformat(),
        "days_left_in_week": max(days_left, 0),
        "unlocked_weeks": unlocked,
        "season_complete": wk >= total_weeks and today > last,
        "before_start": today < start_date,
    }
