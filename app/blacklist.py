"""Blacklist logic: manual + automatic (no-show / excessive cancellations)."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit
from .db import get_setting
from .models import Blacklist, Booking, BookingStatus


async def is_blacklisted(session: AsyncSession, tg_id: int) -> Optional[Blacklist]:
    row = (
        await session.execute(
            select(Blacklist).where(
                Blacklist.tg_id == tg_id, Blacklist.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()
    return row


async def block(
    session: AsyncSession,
    tg_id: int,
    reason: str,
    *,
    actor_tg_id: Optional[int] = None,
    actor_role: str = "system",
) -> None:
    existing = (
        await session.execute(select(Blacklist).where(Blacklist.tg_id == tg_id))
    ).scalar_one_or_none()
    if existing:
        existing.is_active = True
        existing.reason = reason
        existing.created_at = dt.datetime.now(dt.timezone.utc)
    else:
        session.add(Blacklist(tg_id=tg_id, reason=reason, is_active=True))
    await audit.log(
        session,
        "blacklist_add",
        actor_tg_id=actor_tg_id,
        actor_role=actor_role,
        entity="user",
        entity_id=tg_id,
        details=reason,
    )


async def unblock(
    session: AsyncSession,
    tg_id: int,
    *,
    actor_tg_id: Optional[int] = None,
    actor_role: str = "admin",
) -> None:
    row = (
        await session.execute(select(Blacklist).where(Blacklist.tg_id == tg_id))
    ).scalar_one_or_none()
    if row:
        row.is_active = False
    await audit.log(
        session,
        "blacklist_remove",
        actor_tg_id=actor_tg_id,
        actor_role=actor_role,
        entity="user",
        entity_id=tg_id,
    )


async def check_auto_blacklist(session: AsyncSession, tg_id: int) -> bool:
    """Auto-blacklist after N no-shows. Returns True if the client got blocked."""
    threshold = int(await get_setting(session, "no_show_blacklist_threshold", 2))
    no_shows = (
        await session.execute(
            select(func.count(Booking.id)).where(
                Booking.client_tg_id == tg_id,
                Booking.status == BookingStatus.no_show,
            )
        )
    ).scalar_one()
    if no_shows >= threshold:
        await block(
            session,
            tg_id,
            reason=f"Auto: {no_shows} no-shows",
            actor_role="system",
        )
        return True
    return False


async def recent_cancellations(session: AsyncSession, tg_id: int) -> int:
    """Count cancellations within the configured rolling window."""
    window_days = int(await get_setting(session, "cancel_limit_window_days", 30))
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)
    return (
        await session.execute(
            select(func.count(Booking.id)).where(
                Booking.client_tg_id == tg_id,
                Booking.status == BookingStatus.cancelled,
                Booking.cancelled_at >= since,
            )
        )
    ).scalar_one()


async def is_cancel_restricted(session: AsyncSession, tg_id: int) -> bool:
    """True if client exceeded cancellation limit and should be restricted."""
    limit = int(await get_setting(session, "cancel_limit_count", 3))
    return (await recent_cancellations(session, tg_id)) >= limit
