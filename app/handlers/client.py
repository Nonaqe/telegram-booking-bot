"""Сценарий записи клиента + просмотр/отмена своих записей."""
from __future__ import annotations

import datetime as dt

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from .. import blacklist, booking_ops, keyboards, notifications
from ..config import Config
from ..db import get_setting
from ..models import Booking, BookingStatus, Master, Service, User
from ..slots import available_slots, master_window
from ..states import BookingFlow, ClientRescheduleFlow
from ..utils import esc, fmt_dt, money, now_utc, to_local
from .common import BTN_BOOK, BTN_MY

router = Router()


async def _show(msg, text: str, reply_markup=None) -> None:
    """Редактирует сообщение независимо от того, фото это или текст."""
    if msg.photo:
        await msg.edit_caption(caption=text, reply_markup=reply_markup)
    else:
        await msg.edit_text(text, reply_markup=reply_markup)


# ---- вход -------------------------------------------------------------------

@router.message(Command("book"))
@router.message(F.text == BTN_BOOK)
async def start_booking(message: Message, state: FSMContext, session, cfg: Config) -> None:
    await state.clear()
    bl = await blacklist.is_blacklisted(session, message.from_user.id)
    if bl:
        await message.answer(f"🚫 Вы заблокированы для записи.\nПричина: {esc(bl.reason) or 'не указана'}")
        return

    # Анти-бот: при включённой настройке требуем телефон до записи.
    if bool(await get_setting(session, "require_phone", False)):
        user = (
            await session.execute(select(User).where(User.tg_id == message.from_user.id))
        ).scalar_one_or_none()
        if not user or not user.phone:
            await message.answer(
                "Перед записью поделитесь номером телефона — "
                "кнопка «📱 Поделиться телефоном» внизу."
            )
            return

    services = (
        await session.execute(
            select(Service)
            .where(Service.is_active.is_(True))
            .options(selectinload(Service.masters))
            .order_by(Service.name)
        )
    ).scalars().all()
    services = [s for s in services if any(m.is_active for m in s.masters)]
    if not services:
        await message.answer("Сейчас нет доступных услуг.")
        return
    await state.set_state(BookingFlow.service)
    await message.answer(
        "Выберите услугу:",
        reply_markup=keyboards.services_kb(services, cfg.show_prices, cfg.currency),
    )


@router.callback_query(F.data == "book:cancel:0")
async def cancel_flow(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await _show(cb.message, "Запись отменена.")
    await cb.answer()


# ---- услуга -----------------------------------------------------------------

@router.callback_query(BookingFlow.service, F.data.startswith("book:svc:"))
async def pick_service(cb: CallbackQuery, state: FSMContext, session) -> None:
    service_id = int(cb.data.split(":")[2])
    service = await session.get(Service, service_id)
    if not service:
        await cb.answer("Услуга не найдена", show_alert=True)
        return
    await state.update_data(service_id=service_id)
    masters = (
        await session.execute(
            select(Master)
            .where(Master.is_active.is_(True))
            .options(selectinload(Master.services))
            .order_by(Master.name)
        )
    ).scalars().all()
    masters = [m for m in masters if any(s.id == service_id for s in m.services)]
    if not masters:
        await cb.answer("Нет мастера для этой услуги", show_alert=True)
        return
    await state.set_state(BookingFlow.master)
    await cb.message.edit_text("Выберите мастера:", reply_markup=keyboards.masters_kb(masters))
    await cb.answer()


@router.callback_query(F.data == "book:back:svc")
async def back_to_services(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    services = (
        await session.execute(
            select(Service).where(Service.is_active.is_(True))
            .options(selectinload(Service.masters)).order_by(Service.name)
        )
    ).scalars().all()
    services = [s for s in services if any(m.is_active for m in s.masters)]
    await state.set_state(BookingFlow.service)
    await cb.message.edit_text(
        "Выберите услугу:",
        reply_markup=keyboards.services_kb(services, cfg.show_prices, cfg.currency),
    )
    await cb.answer()


# ---- мастер ----------------------------------------------------------------

@router.callback_query(BookingFlow.master, F.data.startswith("book:mst:"))
async def pick_master(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    master_id = int(cb.data.split(":")[2])
    master = await session.get(Master, master_id)
    if not master:
        await cb.answer("Мастер не найден", show_alert=True)
        return
    await state.update_data(master_id=master_id)
    await _show_dates(cb, state, session, cfg, master)


async def _show_dates(cb, state, session, cfg, master) -> None:
    horizon = int(await get_setting(session, "booking_horizon_days", 14))
    today = to_local(now_utc(), cfg.timezone).date()
    dates = []
    for i in range(horizon + 1):
        d = today + dt.timedelta(days=i)
        if master_window(master, d) is not None:
            dates.append(d)
    if not dates:
        await cb.answer("У мастера нет рабочих дней в ближайшее время", show_alert=True)
        return
    await state.set_state(BookingFlow.date)
    await cb.message.edit_text(
        f"Мастер: <b>{master.name}</b>\nВыберите дату:",
        reply_markup=keyboards.dates_kb(dates),
    )
    await cb.answer()


@router.callback_query(F.data == "book:back:mst")
async def back_to_masters(cb: CallbackQuery, state: FSMContext, session) -> None:
    data = await state.get_data()
    service_id = data.get("service_id")
    masters = (
        await session.execute(
            select(Master).where(Master.is_active.is_(True))
            .options(selectinload(Master.services)).order_by(Master.name)
        )
    ).scalars().all()
    masters = [m for m in masters if any(s.id == service_id for s in m.services)]
    await state.set_state(BookingFlow.master)
    await cb.message.edit_text("Выберите мастера:", reply_markup=keyboards.masters_kb(masters))
    await cb.answer()


# ---- дата ------------------------------------------------------------------

@router.callback_query(BookingFlow.date, F.data.startswith("book:day:"))
async def pick_date(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    date = dt.date.fromisoformat(cb.data.split(":", 2)[2])
    data = await state.get_data()
    master = await session.get(Master, data["master_id"])
    service = await session.get(Service, data["service_id"])
    slots = await available_slots(session, master, service, date, cfg.timezone)
    if not slots:
        await cb.answer("В этот день нет свободного времени. Выберите другую дату.", show_alert=True)
        return
    await state.update_data(date=date.isoformat())
    await state.set_state(BookingFlow.slot)
    await cb.message.edit_text(
        f"Дата: <b>{date.strftime('%d.%m.%Y')}</b>\nВыберите время:",
        reply_markup=keyboards.slots_kb(slots, cfg.timezone),
    )
    await cb.answer()


@router.callback_query(F.data == "book:back:day")
async def back_to_dates(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    data = await state.get_data()
    master = await session.get(Master, data["master_id"])
    await _show_dates(cb, state, session, cfg, master)


# ---- время + подтверждение -------------------------------------------------

@router.callback_query(BookingFlow.slot, F.data.startswith("book:slot:"))
async def pick_slot(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    start_utc = dt.datetime.fromisoformat(cb.data.split(":", 2)[2])
    data = await state.get_data()
    master = await session.get(Master, data["master_id"])
    service = await session.get(Service, data["service_id"])
    await state.update_data(start_utc=start_utc.isoformat())
    await state.set_state(BookingFlow.confirm)

    summary = [
        "Подтвердите запись:",
        "",
        f"💈 Услуга: <b>{esc(service.name)}</b>",
        f"👨‍🔧 Мастер: <b>{esc(master.name)}</b>",
        f"🕐 Когда: <b>{fmt_dt(start_utc, cfg.timezone)}</b>",
        f"⏱ Длительность: {service.duration_min} мин",
    ]
    if cfg.show_prices and service.price:
        summary.append(f"💲 Цена: <b>{money(service.price, cfg.currency)}</b>")
    text = "\n".join(summary)
    photo = service.photo_file_id or master.photo_file_id
    if photo:
        await cb.message.delete()
        await cb.message.answer_photo(photo, caption=text, reply_markup=keyboards.confirm_kb())
    else:
        await cb.message.edit_text(text, reply_markup=keyboards.confirm_kb())
    await cb.answer()


@router.callback_query(BookingFlow.confirm, F.data == "book:confirm:1")
async def confirm_booking(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    data = await state.get_data()
    master = await session.get(Master, data["master_id"])
    service = await session.get(Service, data["service_id"])
    start_utc = dt.datetime.fromisoformat(data["start_utc"])
    try:
        booking = await booking_ops.create_booking(
            session,
            client_tg_id=cb.from_user.id,
            client_name=cb.from_user.full_name,
            master=master,
            service=service,
            start_utc=start_utc,
            tz=cfg.timezone,
        )
    except booking_ops.BookingError as e:
        await _show(cb.message, f"❌ {e}")
        await state.clear()
        await cb.answer()
        return

    await state.clear()
    await _show(
        cb.message,
        f"✅ Записано!\n\n💈 {esc(service.name)}\n👨‍🔧 {esc(master.name)}\n"
        f"🕐 {fmt_dt(start_utc, cfg.timezone)}",
    )
    await cb.answer("Запись подтверждена")

    when = fmt_dt(start_utc, cfg.timezone)
    client = esc(cb.from_user.full_name)
    if master.tg_id:
        await notifications.notify(
            master.tg_id,
            f"🆕 Новая запись: {esc(service.name)}\nКлиент: {client}\n🕐 {when}",
        )
    await notifications.notify_admins(
        cfg.admins,
        f"🆕 Запись #{booking.id}: {esc(service.name)} · {esc(master.name)}\n"
        f"Клиент: {client}\n🕐 {when}",
    )


# ---- мои записи ------------------------------------------------------------

@router.message(Command("my"))
@router.message(F.text == BTN_MY)
async def my_bookings(message: Message, state: FSMContext, session, cfg: Config) -> None:
    await state.clear()
    rows = (
        await session.execute(
            select(Booking)
            .where(
                Booking.client_tg_id == message.from_user.id,
                Booking.status.in_([BookingStatus.pending, BookingStatus.confirmed]),
                Booking.start_time >= now_utc(),
            )
            .options(selectinload(Booking.master), selectinload(Booking.service))
            .order_by(Booking.start_time)
        )
    ).scalars().all()
    if not rows:
        await message.answer("У вас нет предстоящих записей.")
        return
    lines = ["<b>Ваши предстоящие записи:</b>", ""]
    for b in rows:
        lines.append(
            f"• {fmt_dt(b.start_time, cfg.timezone)} — {esc(b.service.name)} ({esc(b.master.name)})"
        )
    await message.answer(
        "\n".join(lines), reply_markup=keyboards.my_bookings_kb(rows, cfg.timezone)
    )


@router.callback_query(F.data.startswith("my:cancel:"))
async def cancel_my_booking(cb: CallbackQuery, session, cfg: Config) -> None:
    booking_id = int(cb.data.split(":")[2])
    booking = await session.get(Booking, booking_id)
    if not booking or booking.client_tg_id != cb.from_user.id:
        await cb.answer("Не найдено", show_alert=True)
        return
    if booking.status not in (BookingStatus.pending, BookingStatus.confirmed):
        await cb.answer("Эту запись нельзя отменить", show_alert=True)
        return

    cancel_min_hours = int(await get_setting(session, "cancel_min_hours", 3))
    if booking.start_time - now_utc() < dt.timedelta(hours=cancel_min_hours):
        await cb.answer(
            f"Слишком поздно для отмены (минимум за {cancel_min_hours} ч).", show_alert=True
        )
        return

    master = await session.get(Master, booking.master_id)
    service = await session.get(Service, booking.service_id)
    when = fmt_dt(booking.start_time, cfg.timezone)
    await booking_ops.cancel_booking(
        session, booking, actor_tg_id=cb.from_user.id, actor_role="client"
    )

    if await blacklist.is_cancel_restricted(session, cb.from_user.id):
        await notifications.notify_admins(
            cfg.admins,
            f"⚠️ Клиент {esc(cb.from_user.full_name)} ({cb.from_user.id}) "
            f"превысил лимит отмен.",
        )

    await cb.message.edit_text("❌ Запись отменена.")
    await cb.answer("Отменено")
    if master and master.tg_id:
        await notifications.notify(
            master.tg_id,
            f"❌ Отмена: {esc(service.name) if service else ''}\n🕐 {when}",
        )
    await notifications.notify_admins(cfg.admins, f"❌ Запись #{booking.id} отменена клиентом.")


# ---- перенос записи клиентом -----------------------------------------------

@router.callback_query(F.data.startswith("my:resch:"))
async def my_resch_start(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    booking = await session.get(Booking, int(cb.data.split(":")[2]))
    if not booking or booking.client_tg_id != cb.from_user.id:
        await cb.answer("Не найдено", show_alert=True)
        return
    if booking.status not in (BookingStatus.pending, BookingStatus.confirmed):
        await cb.answer("Эту запись нельзя перенести", show_alert=True)
        return

    # Защита от бесконечных переносов.
    limit = int(await get_setting(session, "max_client_reschedules", 2))
    if limit > 0 and (booking.reschedule_count or 0) >= limit:
        await cb.answer(
            f"Лимит переносов исчерпан ({limit}). Обратитесь в салон.", show_alert=True
        )
        return

    min_hours = int(await get_setting(session, "cancel_min_hours", 3))
    if booking.start_time - now_utc() < dt.timedelta(hours=min_hours):
        await cb.answer(f"Перенос возможен минимум за {min_hours} ч.", show_alert=True)
        return

    master = await session.get(Master, booking.master_id)
    horizon = int(await get_setting(session, "booking_horizon_days", 14))
    today = to_local(now_utc(), cfg.timezone).date()
    dates = [
        today + dt.timedelta(days=i)
        for i in range(horizon + 1)
        if master_window(master, today + dt.timedelta(days=i)) is not None
    ]
    if not dates:
        await cb.answer("У мастера нет рабочих дней", show_alert=True)
        return
    await state.update_data(booking_id=booking.id)
    await state.set_state(ClientRescheduleFlow.date)
    await cb.message.answer(
        "🔁 Перенос. Выберите новую дату:",
        reply_markup=keyboards.dates_kb(dates, prefix="crsch", back="crsch:back"),
    )
    await cb.answer()


@router.callback_query(F.data == "crsch:back")
async def my_resch_back(cb: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await cb.message.edit_text("Перенос отменён.")
    await cb.answer()


@router.callback_query(ClientRescheduleFlow.date, F.data.startswith("crsch:day:"))
async def my_resch_day(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    date = dt.date.fromisoformat(cb.data.split(":", 2)[2])
    data = await state.get_data()
    booking = await session.get(Booking, data["booking_id"])
    master = await session.get(Master, booking.master_id)
    service = await session.get(Service, booking.service_id)
    slots = await available_slots(session, master, service, date, cfg.timezone)
    if not slots:
        await cb.answer("Нет свободного времени, выберите другую дату.", show_alert=True)
        return
    await state.set_state(ClientRescheduleFlow.slot)
    await cb.message.edit_text(
        f"Дата: <b>{date.strftime('%d.%m.%Y')}</b>\nВыберите время:",
        reply_markup=keyboards.slots_kb(slots, cfg.timezone, prefix="crsch", back="crsch:back"),
    )
    await cb.answer()


@router.callback_query(ClientRescheduleFlow.slot, F.data.startswith("crsch:slot:"))
async def my_resch_slot(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    start_utc = dt.datetime.fromisoformat(cb.data.split(":", 2)[2])
    data = await state.get_data()
    booking = await session.get(Booking, data["booking_id"])
    master = await session.get(Master, booking.master_id)
    service = await session.get(Service, booking.service_id)
    try:
        await booking_ops.reschedule_booking(
            session, booking, master, service, start_utc,
            actor_tg_id=cb.from_user.id, actor_role="client", tz=cfg.timezone,
        )
    except booking_ops.BookingError as e:
        await cb.message.edit_text(f"❌ {e}")
        await state.clear()
        await cb.answer()
        return
    await state.clear()
    when = fmt_dt(start_utc, cfg.timezone)
    await cb.message.edit_text(f"✅ Запись перенесена на {when}.")
    await cb.answer("Перенесено")
    if master.tg_id:
        await notifications.notify(master.tg_id, f"🔁 Клиент перенёс запись на {when}.")
    await notifications.notify_admins(cfg.admins, f"🔁 Запись #{booking.id} перенесена клиентом на {when}.")
