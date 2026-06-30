"""Shared helpers: time conversion and formatting.

Convention: all datetimes stored in the DB are NAIVE UTC.
Display/scheduling uses the business timezone from config.
"""
from __future__ import annotations

import datetime as dt
import html

from .tz import ZoneInfo


def esc(value) -> str:
    """HTML-escape любой пользовательский текст перед вставкой в parse_mode=HTML."""
    return html.escape(str(value if value is not None else ""), quote=False)

WEEKDAY_KEYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def now_utc() -> dt.datetime:
    """Current time as naive UTC (matches DB storage convention)."""
    return dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)


def to_local(naive_utc: dt.datetime, tz: str) -> dt.datetime:
    """Convert naive-UTC datetime to aware local datetime."""
    return naive_utc.replace(tzinfo=dt.timezone.utc).astimezone(ZoneInfo(tz))


def local_to_utc_naive(aware_local: dt.datetime) -> dt.datetime:
    """Convert an aware local datetime to naive UTC for storage."""
    return aware_local.astimezone(dt.timezone.utc).replace(tzinfo=None)


def make_local(date: dt.date, time: dt.time, tz: str) -> dt.datetime:
    """Build an aware local datetime from a date + time."""
    return dt.datetime.combine(date, time).replace(tzinfo=ZoneInfo(tz))


def fmt_dt(naive_utc: dt.datetime, tz: str) -> str:
    loc = to_local(naive_utc, tz)
    return loc.strftime("%d.%m.%Y %H:%M")


def fmt_date(naive_utc: dt.datetime, tz: str) -> str:
    return to_local(naive_utc, tz).strftime("%d.%m.%Y")


def fmt_time(naive_utc: dt.datetime, tz: str) -> str:
    return to_local(naive_utc, tz).strftime("%H:%M")


def money(amount: float, currency: str) -> str:
    if amount == int(amount):
        amount_str = str(int(amount))
    else:
        amount_str = f"{amount:.2f}"
    return f"{amount_str} {currency}".strip()
