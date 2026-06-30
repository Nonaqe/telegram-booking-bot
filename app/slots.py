"""Automatic slot generation with buffer handling and overlap prevention."""
from __future__ import annotations

import datetime as dt
import json
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .db import get_setting
from .models import Booking, BookingStatus, Master, Service
from .utils import WEEKDAY_KEYS, local_to_utc_naive, make_local, now_utc


def _parse_window(spec: Optional[str]) -> Optional[tuple[dt.time, dt.time]]:
    if not spec:
        return None
    try:
        start_s, end_s = spec.split("-")
        sh, sm = map(int, start_s.strip().split(":"))
        eh, em = map(int, end_s.strip().split(":"))
        return dt.time(sh, sm), dt.time(eh, em)
    except (ValueError, AttributeError):
        return None


def master_window(master: Master, date: dt.date) -> Optional[tuple[dt.time, dt.time]]:
    """Return (start, end) working times for a master on a date, or None if off."""
    days_off = json.loads(master.days_off or "[]")
    if date.isoformat() in days_off:
        return None
    hours = json.loads(master.working_hours or "{}")
    key = WEEKDAY_KEYS[date.weekday()]
    return _parse_window(hours.get(key))


async def available_slots(
    session: AsyncSession,
    master: Master,
    service: Service,
    date: dt.date,
    tz: str,
) -> List[dt.datetime]:
    """Return available start times (naive UTC) for a master/service/day.

    Respects working hours, buffer between bookings, slot step, min lead time,
    and prevents overlaps with existing active bookings.
    """
    window = master_window(master, date)
    if window is None:
        return []

    buffer_min = int(await get_setting(session, "buffer_minutes", 10))
    step_min = int(await get_setting(session, "slot_step_minutes", 15))
    min_lead_min = int(await get_setting(session, "min_lead_minutes", 60))

    duration = dt.timedelta(minutes=service.duration_min)
    buffer = dt.timedelta(minutes=buffer_min)
    step = dt.timedelta(minutes=step_min)

    win_start = make_local(date, window[0], tz)
    win_end = make_local(date, window[1], tz)

    # Existing active bookings for this master on this day (compare in UTC).
    day_start_utc = local_to_utc_naive(win_start) - dt.timedelta(hours=2)
    day_end_utc = local_to_utc_naive(win_end) + dt.timedelta(hours=2)
    rows = (
        await session.execute(
            select(Booking).where(
                Booking.master_id == master.id,
                Booking.status.in_(
                    [BookingStatus.pending, BookingStatus.confirmed]
                ),
                Booking.start_time >= day_start_utc,
                Booking.start_time <= day_end_utc,
            )
        )
    ).scalars().all()
    busy = [(b.start_time, b.end_time) for b in rows]

    not_before = now_utc() + dt.timedelta(minutes=min_lead_min)

    slots: List[dt.datetime] = []
    cursor = win_start
    while cursor + duration <= win_end:
        start_utc = local_to_utc_naive(cursor)
        end_utc = start_utc + duration

        if start_utc >= not_before:
            # New interval padded by buffer must not overlap existing bookings.
            conflict = False
            for b_start, b_end in busy:
                if start_utc < b_end + buffer and b_start - buffer < end_utc:
                    conflict = True
                    break
            if not conflict:
                slots.append(start_utc)

        cursor += step

    return slots


async def slot_is_free(
    session: AsyncSession,
    master: Master,
    service: Service,
    start_utc: dt.datetime,
    tz: str,
) -> bool:
    """Re-validate a chosen slot at booking time (guards against races)."""
    from .utils import to_local

    local_date = to_local(start_utc, tz).date()
    free = await available_slots(session, master, service, local_date, tz)
    return start_utc in free
