"""Core booking operations shared across client/master/admin handlers."""
from __future__ import annotations

import datetime as dt
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from . import audit, blacklist
from .db import get_setting
from .models import Booking, BookingStatus, Master, Service
from .slots import slot_is_free
from .utils import now_utc


class BookingError(Exception):
    pass


async def create_booking(
    session: AsyncSession,
    *,
    client_tg_id: int,
    client_name: Optional[str],
    master: Master,
    service: Service,
    start_utc: dt.datetime,
    tz: str,
) -> Booking:
    bl = await blacklist.is_blacklisted(session, client_tg_id)
    if bl:
        raise BookingError(f"Вы заблокированы для записи. Причина: {bl.reason or 'не указана'}")

    # Анти-спам: лимит предстоящих записей на клиента.
    max_active = int(await get_setting(session, "max_active_bookings", 3))
    if max_active > 0:
        active = (
            await session.execute(
                select(func.count(Booking.id)).where(
                    Booking.client_tg_id == client_tg_id,
                    Booking.status.in_([BookingStatus.pending, BookingStatus.confirmed]),
                    Booking.start_time >= now_utc(),
                )
            )
        ).scalar_one()
        if active >= max_active:
            raise BookingError(
                f"Достигнут лимит активных записей ({max_active}). "
                "Отмените одну из текущих, чтобы записаться снова."
            )

    # Анти-спам: пауза между созданием записей.
    cooldown = int(await get_setting(session, "booking_cooldown_seconds", 0))
    if cooldown > 0:
        last = (
            await session.execute(
                select(func.max(Booking.created_at)).where(
                    Booking.client_tg_id == client_tg_id
                )
            )
        ).scalar_one()
        if last is not None and (now_utc() - last).total_seconds() < cooldown:
            raise BookingError("Слишком часто. Подождите немного и попробуйте снова.")

    if not await slot_is_free(session, master, service, start_utc, tz):
        raise BookingError("Это время только что заняли. Выберите другое.")

    end_utc = start_utc + dt.timedelta(minutes=service.duration_min)
    booking = Booking(
        client_tg_id=client_tg_id,
        client_name=client_name,
        master_id=master.id,
        service_id=service.id,
        start_time=start_utc,
        end_time=end_utc,
        status=BookingStatus.confirmed,
        price=service.price,
    )
    session.add(booking)
    try:
        await session.flush()
    except IntegrityError:
        # Lost the race against a concurrent booking on the same slot.
        await session.rollback()
        raise BookingError("Это время только что заняли. Выберите другое.")
    await audit.log(
        session,
        "booking_create",
        actor_tg_id=client_tg_id,
        actor_role="client",
        entity="booking",
        entity_id=booking.id,
        details=f"{service.name} with {master.name} @ {start_utc.isoformat()}",
    )
    await session.commit()
    return booking


async def cancel_booking(
    session: AsyncSession,
    booking: Booking,
    *,
    actor_tg_id: int,
    actor_role: str,
) -> None:
    booking.status = BookingStatus.cancelled
    booking.cancelled_at = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    await audit.log(
        session,
        "booking_cancel",
        actor_tg_id=actor_tg_id,
        actor_role=actor_role,
        entity="booking",
        entity_id=booking.id,
    )
    await session.commit()


async def reschedule_booking(
    session: AsyncSession,
    booking: Booking,
    master: Master,
    service: Service,
    new_start_utc: dt.datetime,
    *,
    actor_tg_id: int,
    actor_role: str,
    tz: str,
) -> None:
    if not await slot_is_free(session, master, service, new_start_utc, tz):
        raise BookingError("Новое время недоступно.")
    old = booking.start_time
    booking.start_time = new_start_utc
    booking.end_time = new_start_utc + dt.timedelta(minutes=service.duration_min)
    booking.reminded_24h = False
    booking.reminded_1h = False
    booking.reschedule_count = (booking.reschedule_count or 0) + 1
    await audit.log(
        session,
        "booking_reschedule",
        actor_tg_id=actor_tg_id,
        actor_role=actor_role,
        entity="booking",
        entity_id=booking.id,
        details=f"{old.isoformat()} -> {new_start_utc.isoformat()}",
    )
    await session.commit()


async def set_status(
    session: AsyncSession,
    booking: Booking,
    status: BookingStatus,
    *,
    actor_tg_id: int,
    actor_role: str,
) -> bool:
    """Change status. Returns True if the client got auto-blacklisted."""
    booking.status = status
    await audit.log(
        session,
        f"booking_{status.value}",
        actor_tg_id=actor_tg_id,
        actor_role=actor_role,
        entity="booking",
        entity_id=booking.id,
    )
    blacklisted = False
    if status == BookingStatus.no_show:
        blacklisted = await blacklist.check_auto_blacklist(session, booking.client_tg_id)
    await session.commit()
    return blacklisted
