"""Панель мастера: своё расписание + отметка пришёл / не пришёл."""
from __future__ import annotations

import datetime as dt

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .. import booking_ops, keyboards, notifications
from ..config import Config
from ..models import Booking, BookingStatus, Master, Role
from ..utils import esc, fmt_time, local_to_utc_naive, make_local, now_utc, to_local
from .common import BTN_SCHEDULE

router = Router()


async def _master_for(session, tg_id: int) -> Master | None:
    return (
        await session.execute(select(Master).where(Master.tg_id == tg_id))
    ).scalar_one_or_none()


@router.message(Command("schedule"))
@router.message(F.text == BTN_SCHEDULE)
async def my_schedule(message: Message, state: FSMContext, session, cfg: Config, role: Role) -> None:
    await state.clear()
    if role != Role.master:
        await message.answer("Команда только для мастеров.")
        return
    master = await _master_for(session, message.from_user.id)
    if not master:
        await message.answer("Вы не зарегистрированы как мастер.")
        return

    today = to_local(now_utc(), cfg.timezone).date()
    day_start = local_to_utc_naive(make_local(today, dt.time.min, cfg.timezone))
    day_end = local_to_utc_naive(make_local(today + dt.timedelta(days=1), dt.time.min, cfg.timezone))

    rows = (
        await session.execute(
            select(Booking)
            .where(
                Booking.master_id == master.id,
                Booking.start_time >= day_start,
                Booking.start_time < day_end,
                Booking.status.in_([BookingStatus.pending, BookingStatus.confirmed]),
            )
            .options(selectinload(Booking.service))
            .order_by(Booking.start_time)
        )
    ).scalars().all()

    if not rows:
        await message.answer(f"На сегодня записей нет ({today.strftime('%d.%m.%Y')}).")
        return

    await message.answer(f"<b>Ваше расписание на {today.strftime('%d.%m.%Y')}:</b>")
    for b in rows:
        text = (
            f"🕐 {fmt_time(b.start_time, cfg.timezone)} — {esc(b.service.name)}\n"
            f"👤 {esc(b.client_name or b.client_tg_id)}"
        )
        await message.answer(text, reply_markup=keyboards.master_booking_kb(b))


async def _mark(cb: CallbackQuery, session, cfg: Config, status: BookingStatus) -> None:
    master = await _master_for(session, cb.from_user.id)
    booking_id = int(cb.data.split(":")[2])
    booking = await session.get(Booking, booking_id)
    if not booking or not master or booking.master_id != master.id:
        await cb.answer("Это не ваша запись", show_alert=True)
        return
    blacklisted = await booking_ops.set_status(
        session, booking, status, actor_tg_id=cb.from_user.id, actor_role="master"
    )
    label = "✅ Пришёл" if status == BookingStatus.completed else "🚫 Не пришёл"
    await cb.message.edit_text(f"{cb.message.text}\n\n→ {label}")
    await cb.answer("Сохранено")
    if blacklisted:
        await notifications.notify_admins(
            cfg.admins,
            f"🚫 Клиент {booking.client_tg_id} авто-заблокирован (лимит неявок).",
        )


@router.callback_query(F.data.startswith("mst:done:"))
async def mark_came(cb: CallbackQuery, session, cfg: Config) -> None:
    await _mark(cb, session, cfg, BookingStatus.completed)


@router.callback_query(F.data.startswith("mst:noshow:"))
async def mark_noshow(cb: CallbackQuery, session, cfg: Config) -> None:
    await _mark(cb, session, cfg, BookingStatus.no_show)
