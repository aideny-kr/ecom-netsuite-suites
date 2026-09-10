"""Calendar review cohorts and bounded daily catch-up; no external calls."""

from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ReconciliationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    timezone_name: str = "America/Los_Angeles"
    daily_check_hour: int = Field(default=9, ge=0, le=23, strict=True)
    overlap_minutes: int = Field(default=1440, ge=0, le=10080, strict=True)
    max_slice_days: int = Field(default=1, ge=1, le=7, strict=True)

    @field_validator("timezone_name")
    @classmethod
    def supported_zone(cls, value):
        _zone(value)
        return value


def _zone(name):
    try:
        return ZoneInfo(name)
    except (ValueError, TypeError, ZoneInfoNotFoundError):
        raise ValueError("Choose a valid IANA reporting timezone") from None


def _clock(now):
    if not isinstance(now, datetime) or now.utcoffset() is None:
        raise ValueError("An aware clock is required")


def _midnight(day, zone):
    return datetime.combine(day, time.min, zone).astimezone(timezone.utc)


def review_window(kind, now, timezone_name, *, start_date=None, end_date=None):
    _clock(now)
    zone = _zone(timezone_name)
    today = now.astimezone(zone).date()
    if kind == "yesterday":
        start, end = today - timedelta(days=1), today
    elif kind == "last_week":
        end = today - timedelta(days=today.weekday())
        start = end - timedelta(days=7)
    elif kind == "last_month":
        end = today.replace(day=1)
        start = (end - timedelta(days=1)).replace(day=1)
    elif kind == "custom" and type(start_date) is date and type(end_date) is date:
        start, end = start_date, end_date + timedelta(days=1)
    else:
        raise ValueError("Choose yesterday, last week, last month or a bounded custom period")
    if end > today or not timedelta(0) < end - start <= timedelta(days=31):
        raise ValueError("Choose a completed period of at most 31 calendar days")
    return {"window_start": _midnight(start, zone), "window_end": _midnight(end, zone), "window_basis": "completed_at"}


def scheduled_window(policy, now, successful_end=None):
    _clock(now)
    zone = _zone(policy.timezone_name)
    local = now.astimezone(zone)
    day = local.date() - timedelta(days=int(local.hour < policy.daily_check_hour))
    cutoff = _midnight(day, zone)
    if successful_end is None:
        return _midnight(day - timedelta(days=1), zone), cutoff
    _clock(successful_end)
    if successful_end >= cutoff:
        return None
    start = successful_end - timedelta(minutes=policy.overlap_minutes)
    end = min(cutoff, successful_end + timedelta(days=policy.max_slice_days))
    return start, end
