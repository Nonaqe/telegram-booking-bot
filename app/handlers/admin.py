"""Админ-панель в Telegram: статистика, записи, услуги, мастера, ЧС, настройки."""
from __future__ import annotations

import csv
import datetime as dt
import io
import json

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from .. import audit, blacklist, booking_ops, keyboards, notifications
from ..config import Config, coerce_setting
from ..db import all_settings, set_setting
from ..models import (
    Blacklist,
    Booking,
    BookingStatus,
    DailyStat,
    Master,
    Role,
    Service,
    User,
)
from ..slots import available_slots, master_window
from ..states import (
    AdminBlacklistFlow,
    AdminBroadcastFlow,
    AdminDayOffFlow,
    AdminMasterFlow,
    AdminPhotoFlow,
    AdminRescheduleFlow,
    AdminServiceFlow,
    AdminSettingFlow,
)
from ..utils import (
    WEEKDAY_KEYS,
    esc,
    fmt_dt,
    local_to_utc_naive,
    make_local,
    money,
    now_utc,
    to_local,
)
from .common import BTN_ADMIN, MENU_BUTTONS

router = Router()

PAGE_SIZE = 8
MAX_NAME = 64
MAX_REASON = 200
MAX_BROADCAST = 4000

# Свободный текст в FSM: не команда и не кнопка меню (чтобы сценарий не «съедал»
# нажатия кнопок и команды — они уходят своим обработчикам).
FREE = F.text & ~F.text.startswith("/") & ~F.text.in_(MENU_BUTTONS)


async def _guard(event, role: Role) -> bool:
    if role != Role.admin:
        if isinstance(event, CallbackQuery):
            await event.answer("Только для админов", show_alert=True)
        else:
            await event.answer("Только для админов.")
        return False
    return True


def _parse_date(text: str) -> dt.date:
    t = text.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d.%m.%y"):
        try:
            return dt.datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    raise ValueError("Неверная дата")


# ---- вход ------------------------------------------------------------------

@router.message(Command("admin"))
@router.message(F.text == BTN_ADMIN)
async def admin_home(message: Message, state: FSMContext, role: Role) -> None:
    if not await _guard(message, role):
        return
    await state.clear()
    await message.answer("🛠 <b>Админ-панель</b>", reply_markup=keyboards.admin_main_kb())


@router.callback_query(F.data == "adm:home:0")
async def admin_home_cb(cb: CallbackQuery, role: Role, state: FSMContext) -> None:
    if not await _guard(cb, role):
        return
    await state.clear()
    await cb.message.edit_text("🛠 <b>Админ-панель</b>", reply_markup=keyboards.admin_main_kb())
    await cb.answer()


# ============================================================================
#  СТАТИСТИКА
# ============================================================================

@router.callback_query(F.data == "adm:stats:menu")
async def stats_menu(cb: CallbackQuery, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await cb.message.edit_text("📊 Выберите период:", reply_markup=keyboards.stats_menu_kb())
    await cb.answer()


def _period_range(period: str, tz: str):
    today = to_local(now_utc(), tz).date()
    if period == "day":
        start_date, label = today, today.strftime("%d.%m.%Y")
    elif period == "week":
        start_date, label = today - dt.timedelta(days=6), "за 7 дней"
    else:
        start_date, label = today - dt.timedelta(days=29), "за 30 дней"
    start = local_to_utc_naive(make_local(start_date, dt.time.min, tz))
    end = local_to_utc_naive(make_local(today + dt.timedelta(days=1), dt.time.min, tz))
    return start, end, label, start_date, today


@router.callback_query(F.data.in_({"adm:stats:day", "adm:stats:week", "adm:stats:month"}))
async def show_stats(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    period = cb.data.split(":")[2]
    start, end, label, start_date, today = _period_range(period, cfg.timezone)

    async def count(*statuses: BookingStatus) -> int:
        q = select(func.count(Booking.id)).where(
            Booking.start_time >= start, Booking.start_time < end
        )
        if statuses:
            q = q.where(Booking.status.in_(statuses))
        return (await session.execute(q)).scalar_one()

    completed = await count(BookingStatus.completed)
    cancelled = await count(BookingStatus.cancelled)
    no_show = await count(BookingStatus.no_show)
    active = await count(BookingStatus.pending, BookingStatus.confirmed)

    revenue = (
        await session.execute(
            select(func.coalesce(func.sum(Booking.price), 0.0)).where(
                Booking.start_time >= start,
                Booking.start_time < end,
                Booking.status == BookingStatus.completed,
            )
        )
    ).scalar_one()

    # Прибавляем архив (записи, удалённые retention'ом, но сохранённые как агрегаты).
    arch = (
        await session.execute(
            select(
                func.coalesce(func.sum(DailyStat.completed), 0),
                func.coalesce(func.sum(DailyStat.cancelled), 0),
                func.coalesce(func.sum(DailyStat.no_show), 0),
                func.coalesce(func.sum(DailyStat.revenue), 0.0),
            ).where(
                DailyStat.date >= start_date.isoformat(),
                DailyStat.date <= today.isoformat(),
            )
        )
    ).one()
    completed += int(arch[0]); cancelled += int(arch[1]); no_show += int(arch[2])
    revenue += float(arch[3])
    total = active + completed + cancelled + no_show

    # Загрузка мастеров (по «живым» записям).
    load_rows = (
        await session.execute(
            select(Master.name, func.count(Booking.id))
            .join(Booking, Booking.master_id == Master.id)
            .where(
                Booking.start_time >= start,
                Booking.start_time < end,
                Booking.status.in_(
                    [BookingStatus.confirmed, BookingStatus.completed, BookingStatus.pending]
                ),
            )
            .group_by(Master.name)
            .order_by(func.count(Booking.id).desc())
        )
    ).all()

    lines = [
        f"📊 <b>Статистика — {label}</b>",
        "",
        f"Всего записей: <b>{total}</b>",
        f"Активные/предстоящие: {active}",
        f"Выполнено: {completed}",
        f"Отменено: {cancelled}",
        f"Неявки: {no_show}",
    ]
    if cfg.show_prices:
        lines.append(f"💲 Доход (выполненные): <b>{money(revenue, cfg.currency)}</b>")
    lines.append("")
    lines.append("<b>Загрузка мастеров:</b>")
    if load_rows:
        for name, c in load_rows:
            lines.append(f"• {esc(name)}: {c}")
    else:
        lines.append("—")

    await cb.message.edit_text("\n".join(lines), reply_markup=keyboards.stats_menu_kb())
    await cb.answer()


# ============================================================================
#  ЗАПИСИ
# ============================================================================

@router.callback_query(F.data == "adm:bk:menu")
async def bookings_menu(cb: CallbackQuery, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await cb.message.edit_text("📅 Записи:", reply_markup=keyboards.bookings_menu_kb())
    await cb.answer()


async def _render_cards(target: Message, session, cfg, bookings) -> None:
    for b in bookings:
        text = (
            f"#{b.id} · {b.status.value}\n"
            f"🕐 {fmt_dt(b.start_time, cfg.timezone)}\n"
            f"💈 {esc(b.service.name)} · 👨‍🔧 {esc(b.master.name)}\n"
            f"👤 {esc(b.client_name or b.client_tg_id)}"
        )
        kb = None
        if b.status in (BookingStatus.pending, BookingStatus.confirmed):
            kb = keyboards.admin_booking_actions_kb(b)
        await target.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("adm:bk:all:"))
async def bookings_all(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    page = int(cb.data.split(":")[3])
    total = (
        await session.execute(
            select(func.count(Booking.id)).where(
                Booking.status.in_([BookingStatus.pending, BookingStatus.confirmed]),
                Booking.start_time >= now_utc(),
            )
        )
    ).scalar_one()
    rows = (
        await session.execute(
            select(Booking)
            .where(
                Booking.status.in_([BookingStatus.pending, BookingStatus.confirmed]),
                Booking.start_time >= now_utc(),
            )
            .options(selectinload(Booking.master), selectinload(Booking.service))
            .order_by(Booking.start_time)
            .offset(page * PAGE_SIZE)
            .limit(PAGE_SIZE)
        )
    ).scalars().all()

    if page == 0:
        if not rows:
            await cb.message.edit_text("Записей нет.", reply_markup=keyboards.back_to_admin_kb())
            await cb.answer()
            return
        await cb.message.edit_text(
            f"📅 <b>Предстоящие записи</b> (всего {total})"
        )
    await _render_cards(cb.message, session, cfg, rows)
    has_next = (page + 1) * PAGE_SIZE < total
    await cb.message.answer(
        f"Стр. {page + 1}", reply_markup=keyboards.pager_kb("adm:bk:all", page, has_next)
    )
    await cb.answer()


@router.callback_query(F.data == "adm:bk:today")
async def bookings_today(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    today = to_local(now_utc(), cfg.timezone).date()
    start = local_to_utc_naive(make_local(today, dt.time.min, cfg.timezone))
    end = local_to_utc_naive(make_local(today + dt.timedelta(days=1), dt.time.min, cfg.timezone))
    rows = (
        await session.execute(
            select(Booking)
            .where(Booking.start_time >= start, Booking.start_time < end)
            .options(selectinload(Booking.master), selectinload(Booking.service))
            .order_by(Booking.start_time)
        )
    ).scalars().all()
    if not rows:
        await cb.message.edit_text("На сегодня записей нет.", reply_markup=keyboards.back_to_admin_kb())
        await cb.answer()
        return
    await cb.message.edit_text(f"📆 <b>Записи на {today.strftime('%d.%m.%Y')}</b>")
    await _render_cards(cb.message, session, cfg, rows)
    await cb.message.answer("—", reply_markup=keyboards.back_to_admin_kb())
    await cb.answer()


@router.callback_query(F.data == "adm:bk:bymaster")
async def bookings_by_master(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    masters = (await session.execute(select(Master).order_by(Master.name))).scalars().all()
    await cb.message.edit_text(
        "Выберите мастера:",
        reply_markup=keyboards.admin_masters_pick_kb(masters, "bk:master"),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm:bk:master:"))
async def bookings_master_list(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    master_id = int(cb.data.split(":")[3])
    rows = (
        await session.execute(
            select(Booking)
            .where(
                Booking.master_id == master_id,
                Booking.status.in_([BookingStatus.pending, BookingStatus.confirmed]),
                Booking.start_time >= now_utc(),
            )
            .options(selectinload(Booking.master), selectinload(Booking.service))
            .order_by(Booking.start_time)
            .limit(30)
        )
    ).scalars().all()
    if not rows:
        await cb.message.edit_text("Записей нет.", reply_markup=keyboards.back_to_admin_kb())
        await cb.answer()
        return
    await cb.message.edit_text("📅 <b>Записи мастера</b>")
    await _render_cards(cb.message, session, cfg, rows)
    await cb.message.answer("—", reply_markup=keyboards.back_to_admin_kb())
    await cb.answer()


async def _admin_change_status(cb, session, cfg, status: BookingStatus) -> None:
    booking_id = int(cb.data.split(":")[3])
    booking = await session.get(Booking, booking_id)
    if not booking:
        await cb.answer("Не найдено", show_alert=True)
        return
    master = await session.get(Master, booking.master_id)
    when = fmt_dt(booking.start_time, cfg.timezone)

    if status == BookingStatus.cancelled:
        await booking_ops.cancel_booking(
            session, booking, actor_tg_id=cb.from_user.id, actor_role="admin"
        )
        await notifications.notify(
            booking.client_tg_id, f"❌ Ваша запись на {when} отменена салоном."
        )
        if master and master.tg_id:
            await notifications.notify(master.tg_id, f"❌ Запись отменена: {when}")
    else:
        blacklisted = await booking_ops.set_status(
            session, booking, status, actor_tg_id=cb.from_user.id, actor_role="admin"
        )
        if blacklisted:
            await notifications.notify_admins(
                cfg.admins, f"🚫 Клиент {booking.client_tg_id} авто-заблокирован (неявки)."
            )
    await cb.message.edit_text(f"{cb.message.text}\n\n→ {status.value}")
    await cb.answer("Обновлено")


@router.callback_query(F.data.startswith("adm:bk:cancel:"))
async def admin_cancel(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await _admin_change_status(cb, session, cfg, BookingStatus.cancelled)


@router.callback_query(F.data.startswith("adm:bk:done:"))
async def admin_done(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await _admin_change_status(cb, session, cfg, BookingStatus.completed)


@router.callback_query(F.data.startswith("adm:bk:noshow:"))
async def admin_noshow(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await _admin_change_status(cb, session, cfg, BookingStatus.no_show)


# ---- перенос записи --------------------------------------------------------

@router.callback_query(F.data.startswith("adm:bk:resch:"))
async def resch_start(cb: CallbackQuery, state: FSMContext, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    booking = await session.get(Booking, int(cb.data.split(":")[3]))
    if not booking or booking.status not in (BookingStatus.pending, BookingStatus.confirmed):
        await cb.answer("Запись недоступна для переноса", show_alert=True)
        return
    master = await session.get(Master, booking.master_id)
    horizon = 30
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
    await state.set_state(AdminRescheduleFlow.date)
    await cb.message.answer(
        "🔁 Перенос. Выберите новую дату:",
        reply_markup=keyboards.dates_kb(dates, prefix="rsch", back="adm:home:0"),
    )
    await cb.answer()


@router.callback_query(AdminRescheduleFlow.date, F.data.startswith("rsch:day:"))
async def resch_day(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    date = dt.date.fromisoformat(cb.data.split(":", 2)[2])
    data = await state.get_data()
    booking = await session.get(Booking, data["booking_id"])
    master = await session.get(Master, booking.master_id)
    service = await session.get(Service, booking.service_id)
    slots = await available_slots(session, master, service, date, cfg.timezone)
    if not slots:
        await cb.answer("Нет свободного времени, выберите другую дату.", show_alert=True)
        return
    await state.update_data(date=date.isoformat())
    await state.set_state(AdminRescheduleFlow.slot)
    await cb.message.edit_text(
        f"Дата: <b>{date.strftime('%d.%m.%Y')}</b>\nВыберите время:",
        reply_markup=keyboards.slots_kb(slots, cfg.timezone, prefix="rsch", back="adm:home:0"),
    )
    await cb.answer()


@router.callback_query(AdminRescheduleFlow.slot, F.data.startswith("rsch:slot:"))
async def resch_slot(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    start_utc = dt.datetime.fromisoformat(cb.data.split(":", 2)[2])
    data = await state.get_data()
    booking = await session.get(Booking, data["booking_id"])
    master = await session.get(Master, booking.master_id)
    service = await session.get(Service, booking.service_id)
    try:
        await booking_ops.reschedule_booking(
            session, booking, master, service, start_utc,
            actor_tg_id=cb.from_user.id, actor_role="admin", tz=cfg.timezone,
        )
    except booking_ops.BookingError as e:
        await cb.message.edit_text(f"❌ {e}")
        await state.clear()
        await cb.answer()
        return
    await state.clear()
    when = fmt_dt(start_utc, cfg.timezone)
    await cb.message.edit_text(f"✅ Перенесено на {when}.", reply_markup=keyboards.back_to_admin_kb())
    await cb.answer("Готово")
    await notifications.notify(booking.client_tg_id, f"🔁 Ваша запись перенесена на {when}.")
    if master.tg_id:
        await notifications.notify(master.tg_id, f"🔁 Запись перенесена на {when}.")


# ============================================================================
#  УСЛУГИ
# ============================================================================

@router.callback_query(F.data == "adm:svc:list")
async def svc_list(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    services = (await session.execute(select(Service).order_by(Service.name))).scalars().all()
    await cb.message.edit_text(
        "💈 <b>Услуги</b>",
        reply_markup=keyboards.admin_services_list_kb(services, cfg.currency),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm:svc:view:"))
async def svc_view(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    s = await session.get(Service, int(cb.data.split(":")[3]))
    if not s:
        await cb.answer("Не найдено", show_alert=True)
        return
    text = (
        f"💈 <b>{esc(s.name)}</b>\n"
        f"⏱ {s.duration_min} мин\n"
        f"💲 {money(s.price, cfg.currency)}\n"
        f"Активна: {'да' if s.is_active else 'нет'}\n"
        f"Фото: {'есть' if s.photo_file_id else 'нет'}"
    )
    await cb.message.edit_text(text, reply_markup=keyboards.admin_service_view_kb(s))
    await cb.answer()


@router.callback_query(F.data.startswith("adm:svc:toggle:"))
async def svc_toggle(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    s = await session.get(Service, int(cb.data.split(":")[3]))
    s.is_active = not s.is_active
    await audit.log(session, "service_toggle", actor_tg_id=cb.from_user.id,
                    actor_role="admin", entity="service", entity_id=s.id,
                    details=f"active={s.is_active}")
    await session.commit()
    await svc_view(cb, session, cfg, role)


@router.callback_query(F.data.startswith("adm:svc:del:"))
async def svc_del_confirm(cb: CallbackQuery, role: Role) -> None:
    if not await _guard(cb, role):
        return
    sid = int(cb.data.split(":")[3])
    await cb.message.edit_text(
        "Удалить услугу? Это нельзя отменить.",
        reply_markup=keyboards.confirm_delete_kb("svc", sid, "adm:svc:list"),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm:svc:delyes:"))
async def svc_del(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    s = await session.get(Service, int(cb.data.split(":")[3]))
    if s:
        used = (
            await session.execute(
                select(func.count(Booking.id)).where(Booking.service_id == s.id)
            )
        ).scalar_one()
        if used:
            await cb.answer(
                "Есть записи с этой услугой — нельзя удалить. Выключите её вместо удаления.",
                show_alert=True,
            )
            return
        sid = s.id
        await session.delete(s)
        await audit.log(session, "service_delete", actor_tg_id=cb.from_user.id,
                        actor_role="admin", entity="service", entity_id=sid)
        await session.commit()
    services = (await session.execute(select(Service).order_by(Service.name))).scalars().all()
    await cb.message.edit_text(
        "💈 <b>Услуги</b> — удалено.",
        reply_markup=keyboards.admin_services_list_kb(services, cfg.currency),
    )
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("adm:svc:price:"))
async def svc_price_ask(cb: CallbackQuery, state: FSMContext, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await state.update_data(svc_id=int(cb.data.split(":")[3]), field="price")
    await state.set_state(AdminServiceFlow.edit_value)
    await cb.message.edit_text("Отправьте новую цену (число):")
    await cb.answer()


@router.callback_query(F.data.startswith("adm:svc:dur:"))
async def svc_dur_ask(cb: CallbackQuery, state: FSMContext, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await state.update_data(svc_id=int(cb.data.split(":")[3]), field="duration")
    await state.set_state(AdminServiceFlow.edit_value)
    await cb.message.edit_text("Отправьте новую длительность в минутах (целое):")
    await cb.answer()


@router.message(AdminServiceFlow.edit_value, FREE)
async def svc_edit_value(message: Message, state: FSMContext, session, cfg: Config) -> None:
    data = await state.get_data()
    s = await session.get(Service, data["svc_id"])
    if not s:
        await state.clear()
        await message.answer("Услуга не найдена.")
        return
    try:
        if data["field"] == "price":
            s.price = float(message.text.replace(",", "."))
        else:
            s.duration_min = int(message.text)
    except ValueError:
        await message.answer("Неверное число, попробуйте ещё раз:")
        return
    await audit.log(session, "service_edit", actor_tg_id=message.from_user.id,
                    actor_role="admin", entity="service", entity_id=s.id,
                    details=f"{data['field']}")
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Обновлено.\n💈 {s.name}\n⏱ {s.duration_min} мин · 💲 {money(s.price, cfg.currency)}",
        reply_markup=keyboards.admin_service_view_kb(s),
    )


@router.callback_query(F.data == "adm:svc:add")
async def svc_add_start(cb: CallbackQuery, state: FSMContext, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await state.set_state(AdminServiceFlow.name)
    await cb.message.edit_text("Новая услуга — отправьте название:")
    await cb.answer()


@router.message(AdminServiceFlow.name, FREE)
async def svc_add_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name or len(name) > MAX_NAME:
        await message.answer(f"Название: 1–{MAX_NAME} символов. Ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(AdminServiceFlow.duration)
    await message.answer("Длительность в минутах (целое):")


@router.message(AdminServiceFlow.duration, FREE)
async def svc_add_duration(message: Message, state: FSMContext) -> None:
    try:
        await state.update_data(duration=int(message.text))
    except ValueError:
        await message.answer("Отправьте целое число:")
        return
    await state.set_state(AdminServiceFlow.price)
    await message.answer("Цена (число, 0 если без цены):")


@router.message(AdminServiceFlow.price, FREE)
async def svc_add_price(message: Message, state: FSMContext, session, cfg: Config) -> None:
    try:
        price = float(message.text.replace(",", "."))
    except ValueError:
        await message.answer("Отправьте число:")
        return
    data = await state.get_data()
    s = Service(name=data["name"], duration_min=data["duration"], price=price)
    session.add(s)
    await session.flush()
    await audit.log(session, "service_create", actor_tg_id=message.from_user.id,
                    actor_role="admin", entity="service", entity_id=s.id, details=s.name)
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Услуга добавлена: {s.name} ({s.duration_min} мин · {money(s.price, cfg.currency)}).\n"
        "Привяжите её к мастеру: 👨‍🔧 Мастера → Услуги.",
        reply_markup=keyboards.back_to_admin_kb(),
    )


# ============================================================================
#  МАСТЕРА
# ============================================================================

@router.callback_query(F.data == "adm:mst:list")
async def mst_list(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    masters = (await session.execute(select(Master).order_by(Master.name))).scalars().all()
    await cb.message.edit_text(
        "👨‍🔧 <b>Мастера</b>", reply_markup=keyboards.admin_masters_list_kb(masters)
    )
    await cb.answer()


async def _master_card(s: Master) -> str:
    hours = json.loads(s.working_hours or "{}")
    hours_str = ", ".join(f"{k}:{v}" for k, v in hours.items() if v) or "—"
    return (
        f"👨‍🔧 <b>{esc(s.name)}</b>\n"
        f"TG id: {s.tg_id or '—'}\n"
        f"Активен: {'да' if s.is_active else 'нет'}\n"
        f"Услуги: {esc(', '.join(x.name for x in s.services)) or '—'}\n"
        f"Часы: {esc(hours_str)}\n"
        f"Фото: {'есть' if s.photo_file_id else 'нет'}"
    )


@router.callback_query(F.data.startswith("adm:mst:view:"))
async def mst_view(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    m = (
        await session.execute(
            select(Master).where(Master.id == int(cb.data.split(":")[3]))
            .options(selectinload(Master.services))
        )
    ).scalar_one_or_none()
    if not m:
        await cb.answer("Не найдено", show_alert=True)
        return
    await cb.message.edit_text(await _master_card(m), reply_markup=keyboards.admin_master_view_kb(m))
    await cb.answer()


@router.callback_query(F.data.startswith("adm:mst:toggle:"))
async def mst_toggle(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    m = await session.get(Master, int(cb.data.split(":")[3]))
    m.is_active = not m.is_active
    await audit.log(session, "master_toggle", actor_tg_id=cb.from_user.id, actor_role="admin",
                    entity="master", entity_id=m.id, details=f"active={m.is_active}")
    await session.commit()
    await mst_view(cb, session, role)


@router.callback_query(F.data.startswith("adm:mst:del:"))
async def mst_del_confirm(cb: CallbackQuery, role: Role) -> None:
    if not await _guard(cb, role):
        return
    mid = int(cb.data.split(":")[3])
    await cb.message.edit_text(
        "Удалить мастера? Это нельзя отменить.",
        reply_markup=keyboards.confirm_delete_kb("mst", mid, "adm:mst:list"),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm:mst:delyes:"))
async def mst_del(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    m = await session.get(Master, int(cb.data.split(":")[3]))
    if m:
        used = (
            await session.execute(
                select(func.count(Booking.id)).where(Booking.master_id == m.id)
            )
        ).scalar_one()
        if used:
            await cb.answer(
                "У мастера есть записи — нельзя удалить. Выключите его вместо удаления.",
                show_alert=True,
            )
            return
        mid = m.id
        await session.delete(m)
        await audit.log(session, "master_delete", actor_tg_id=cb.from_user.id,
                        actor_role="admin", entity="master", entity_id=mid)
        await session.commit()
    masters = (await session.execute(select(Master).order_by(Master.name))).scalars().all()
    await cb.message.edit_text(
        "👨‍🔧 <b>Мастера</b> — удалено.",
        reply_markup=keyboards.admin_masters_list_kb(masters),
    )
    await cb.answer("Удалено")


@router.callback_query(F.data.startswith("adm:mst:svc:"))
async def mst_services(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    m = (
        await session.execute(
            select(Master).where(Master.id == int(cb.data.split(":")[3]))
            .options(selectinload(Master.services))
        )
    ).scalar_one_or_none()
    services = (await session.execute(select(Service).order_by(Service.name))).scalars().all()
    await cb.message.edit_text(
        f"Услуги мастера <b>{esc(m.name)}</b>:",
        reply_markup=keyboards.admin_master_services_kb(m, services),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm:mst:svctoggle:"))
async def mst_service_toggle(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    _, _, _, mid, sid = cb.data.split(":")
    m = (
        await session.execute(
            select(Master).where(Master.id == int(mid)).options(selectinload(Master.services))
        )
    ).scalar_one_or_none()
    s = await session.get(Service, int(sid))
    if m and s:
        if any(x.id == s.id for x in m.services):
            m.services = [x for x in m.services if x.id != s.id]
        else:
            m.services.append(s)
        await session.commit()
    services = (await session.execute(select(Service).order_by(Service.name))).scalars().all()
    await cb.message.edit_reply_markup(
        reply_markup=keyboards.admin_master_services_kb(m, services)
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm:mst:hours:"))
async def mst_hours_ask(cb: CallbackQuery, state: FSMContext, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await state.update_data(mst_id=int(cb.data.split(":")[3]))
    await state.set_state(AdminMasterFlow.hours)
    await cb.message.edit_text(
        "Отправьте рабочие часы — 7 значений через пробел, по дням пн..вс. "
        "Формат ЧЧ:ММ-ЧЧ:ММ или 'off'.\n\n"
        "Пример:\n<code>10:00-19:00 10:00-19:00 10:00-19:00 "
        "10:00-19:00 10:00-19:00 11:00-16:00 off</code>"
    )
    await cb.answer()


@router.message(AdminMasterFlow.hours, FREE)
async def mst_hours_set(message: Message, state: FSMContext, session) -> None:
    parts = message.text.split()
    if len(parts) != 7:
        await message.answer("Нужно ровно 7 значений (пн..вс). Попробуйте ещё раз:")
        return
    hours = {}
    for key, spec in zip(WEEKDAY_KEYS, parts):
        hours[key] = None if spec.lower() in ("off", "выходной", "-") else spec
    data = await state.get_data()
    m = await session.get(Master, data["mst_id"])
    m.working_hours = json.dumps(hours)
    await audit.log(session, "master_hours", actor_tg_id=message.from_user.id,
                    actor_role="admin", entity="master", entity_id=m.id)
    await session.commit()
    await state.clear()
    await message.answer("✅ Часы обновлены.", reply_markup=keyboards.back_to_admin_kb())


@router.callback_query(F.data == "adm:mst:add")
async def mst_add_start(cb: CallbackQuery, state: FSMContext, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await state.set_state(AdminMasterFlow.name)
    await cb.message.edit_text("Новый мастер — отправьте имя:")
    await cb.answer()


@router.message(AdminMasterFlow.name, FREE)
async def mst_add_name(message: Message, state: FSMContext) -> None:
    name = message.text.strip()
    if not name or len(name) > MAX_NAME:
        await message.answer(f"Имя: 1–{MAX_NAME} символов. Ещё раз:")
        return
    await state.update_data(name=name)
    await state.set_state(AdminMasterFlow.tg_id)
    await message.answer(
        "Отправьте числовой Telegram id мастера (чтобы он получил роль МАСТЕР), "
        "или <code>skip</code>:"
    )


@router.message(AdminMasterFlow.tg_id, FREE)
async def mst_add_tg(message: Message, state: FSMContext, session) -> None:
    text = message.text.strip().lower()
    tg_id = None
    if text not in ("skip", "пропустить", "-"):
        try:
            tg_id = int(text)
        except ValueError:
            await message.answer("Отправьте число или 'skip':")
            return
    data = await state.get_data()
    default_hours = {k: "10:00-19:00" for k in ["mon", "tue", "wed", "thu", "fri"]}
    default_hours.update({"sat": None, "sun": None})
    m = Master(
        name=data["name"],
        tg_id=tg_id,
        working_hours=json.dumps(default_hours),
        days_off="[]",
    )
    session.add(m)
    await session.flush()
    await audit.log(session, "master_create", actor_tg_id=message.from_user.id,
                    actor_role="admin", entity="master", entity_id=m.id, details=m.name)
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Мастер добавлен: {esc(m.name)}.\nЧасы по умолчанию пн–пт 10:00–19:00. "
        "Часы и услуги настройте в 👨‍🔧 Мастера.",
        reply_markup=keyboards.back_to_admin_kb(),
    )


# ---- выходные дни ----------------------------------------------------------

@router.callback_query(F.data.startswith("adm:mst:dayoff:"))
async def dayoff_list(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    m = await session.get(Master, int(cb.data.split(":")[3]))
    days = json.loads(m.days_off or "[]")
    await cb.message.edit_text(
        f"🚫 Выходные дни <b>{esc(m.name)}</b>:" + ("" if days else "\nпока нет"),
        reply_markup=keyboards.admin_dayoff_kb(m, days),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm:mst:dayoffadd:"))
async def dayoff_add_ask(cb: CallbackQuery, state: FSMContext, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await state.update_data(mst_id=int(cb.data.split(":")[3]))
    await state.set_state(AdminDayOffFlow.date)
    await cb.message.edit_text("Отправьте дату выходного (ДД.ММ.ГГГГ):")
    await cb.answer()


@router.message(AdminDayOffFlow.date, FREE)
async def dayoff_add_set(message: Message, state: FSMContext, session) -> None:
    try:
        d = _parse_date(message.text)
    except ValueError:
        await message.answer("Неверная дата. Формат ДД.ММ.ГГГГ:")
        return
    data = await state.get_data()
    m = await session.get(Master, data["mst_id"])
    days = json.loads(m.days_off or "[]")
    if d.isoformat() not in days:
        days.append(d.isoformat())
        days.sort()
    m.days_off = json.dumps(days)
    await audit.log(session, "master_dayoff_add", actor_tg_id=message.from_user.id,
                    actor_role="admin", entity="master", entity_id=m.id, details=d.isoformat())
    await session.commit()
    await state.clear()
    await message.answer(
        f"✅ Выходной добавлен: {d.strftime('%d.%m.%Y')}",
        reply_markup=keyboards.admin_dayoff_kb(m, days),
    )


@router.callback_query(F.data.startswith("adm:mst:dayoffdel:"))
async def dayoff_del(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    _, _, _, mid, date = cb.data.split(":")
    m = await session.get(Master, int(mid))
    days = [x for x in json.loads(m.days_off or "[]") if x != date]
    m.days_off = json.dumps(days)
    await session.commit()
    await cb.message.edit_text(
        f"🚫 Выходные дни <b>{esc(m.name)}</b>:" + ("" if days else "\nпока нет"),
        reply_markup=keyboards.admin_dayoff_kb(m, days),
    )
    await cb.answer("Удалено")


# ============================================================================
#  ЧЁРНЫЙ СПИСОК
# ============================================================================

@router.callback_query(F.data == "adm:bl:list")
async def bl_list(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    rows = (
        await session.execute(
            select(Blacklist).where(Blacklist.is_active.is_(True)).order_by(Blacklist.created_at.desc())
        )
    ).scalars().all()
    lines = ["🚫 <b>Чёрный список</b>", ""]
    if rows:
        for r in rows:
            lines.append(f"• {r.tg_id} — {esc(r.reason) or 'не указана'}")
    else:
        lines.append("Пусто.")
    await cb.message.edit_text(
        "\n".join(lines),
        reply_markup=keyboards.admin_blacklist_kb([(r.tg_id, r.reason) for r in rows]),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm:bl:unblock:"))
async def bl_unblock(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    tg_id = int(cb.data.split(":")[3])
    await blacklist.unblock(session, tg_id, actor_tg_id=cb.from_user.id, actor_role="admin")
    await session.commit()
    await cb.answer("Разблокирован")
    await bl_list(cb, session, role)


@router.callback_query(F.data == "adm:bl:add")
async def bl_add_ask(cb: CallbackQuery, state: FSMContext, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await state.set_state(AdminBlacklistFlow.tg_id)
    await cb.message.edit_text("Отправьте Telegram id для блокировки:")
    await cb.answer()


@router.message(AdminBlacklistFlow.tg_id, FREE)
async def bl_add_id(message: Message, state: FSMContext) -> None:
    try:
        tg_id = int(message.text.strip())
    except ValueError:
        await message.answer("Отправьте числовой id:")
        return
    await state.update_data(tg_id=tg_id)
    await state.set_state(AdminBlacklistFlow.reason)
    await message.answer("Причина (или 'skip'):")


@router.message(AdminBlacklistFlow.reason, FREE)
async def bl_add_reason(message: Message, state: FSMContext, session) -> None:
    data = await state.get_data()
    reason = "" if message.text.strip().lower() in ("skip", "пропустить") else message.text.strip()[:MAX_REASON]
    await blacklist.block(
        session, data["tg_id"], reason or "Ручная блокировка",
        actor_tg_id=message.from_user.id, actor_role="admin",
    )
    await session.commit()
    await state.clear()
    await message.answer("✅ Заблокирован.", reply_markup=keyboards.back_to_admin_kb())


# ============================================================================
#  НАСТРОЙКИ
# ============================================================================

@router.callback_query(F.data == "adm:set:list")
async def set_list(cb: CallbackQuery, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    settings = await all_settings(session)
    await cb.message.edit_text(
        "⚙️ <b>Настройки</b>\nНажмите ключ для изменения:",
        reply_markup=keyboards.admin_settings_kb(settings),
    )
    await cb.answer()


@router.callback_query(F.data.startswith("adm:set:edit:"))
async def set_edit_ask(cb: CallbackQuery, state: FSMContext, session, role: Role) -> None:
    if not await _guard(cb, role):
        return
    key = cb.data.split(":", 3)[3]
    settings = await all_settings(session)
    await state.update_data(key=key)
    await state.set_state(AdminSettingFlow.value)
    await cb.message.edit_text(
        f"Настройка <b>{key}</b>\nСейчас: <code>{settings.get(key)}</code>\n\n"
        "Отправьте новое значение (число / да-нет):"
    )
    await cb.answer()


@router.message(AdminSettingFlow.value, FREE)
async def set_edit_value(message: Message, state: FSMContext, session) -> None:
    data = await state.get_data()
    try:
        value = coerce_setting(data["key"], message.text)
    except ValueError as e:
        await message.answer(f"{e} Попробуйте ещё раз:")
        return
    await set_setting(session, data["key"], value)
    await audit.log(session, "setting_edit", actor_tg_id=message.from_user.id,
                    actor_role="admin", entity="setting", details=f"{data['key']}={value}")
    await session.commit()
    await state.clear()
    settings = await all_settings(session)
    await message.answer(
        f"✅ {data['key']} = {value}",
        reply_markup=keyboards.admin_settings_kb(settings),
    )


# ============================================================================
#  ЭКСПОРТ CSV
# ============================================================================

@router.callback_query(F.data == "adm:bk:export")
async def bk_export(cb: CallbackQuery, session, cfg: Config, role: Role) -> None:
    if not await _guard(cb, role):
        return
    rows = (
        await session.execute(
            select(Booking)
            .options(selectinload(Booking.master), selectinload(Booking.service))
            .order_by(Booking.start_time.desc())
            .limit(5000)
        )
    ).scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["id", "статус", "начало", "конец", "услуга", "мастер",
                "клиент", "tg_id", "цена"])
    for b in rows:
        w.writerow([
            b.id, b.status.value,
            fmt_dt(b.start_time, cfg.timezone), fmt_dt(b.end_time, cfg.timezone),
            b.service.name if b.service else "", b.master.name if b.master else "",
            b.client_name or "", b.client_tg_id, b.price,
        ])
    # utf-8-sig — чтобы кириллица корректно открывалась в Excel.
    data = buf.getvalue().encode("utf-8-sig")
    fname = f"bookings-{dt.datetime.now().strftime('%Y%m%d')}.csv"
    await cb.message.answer_document(
        BufferedInputFile(data, filename=fname),
        caption=f"📤 Экспорт: {len(rows)} записей",
    )
    await cb.answer()


# ============================================================================
#  РАССЫЛКА
# ============================================================================

@router.callback_query(F.data == "adm:bc:start")
async def bc_start(cb: CallbackQuery, state: FSMContext, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await state.set_state(AdminBroadcastFlow.text)
    await cb.message.edit_text(
        "📢 Отправьте текст рассылки (он уйдёт всем пользователям).\n"
        "Для отмены — /cancel."
    )
    await cb.answer()


@router.message(AdminBroadcastFlow.text, FREE)
async def bc_text(message: Message, state: FSMContext, session) -> None:
    text = (message.text or "").strip()[:MAX_BROADCAST]
    if not text:
        await message.answer("Пустой текст. Отправьте сообщение или /cancel:")
        return
    count = (await session.execute(select(func.count(User.id)))).scalar_one()
    await state.update_data(text=text)
    await state.set_state(AdminBroadcastFlow.confirm)
    await message.answer(
        f"Предпросмотр (получателей: {count}):\n\n{esc(text)}",
        reply_markup=keyboards.broadcast_confirm_kb(),
    )


@router.callback_query(AdminBroadcastFlow.confirm, F.data == "adm:bc:send")
async def bc_send(cb: CallbackQuery, state: FSMContext, session, cfg: Config) -> None:
    import asyncio

    data = await state.get_data()
    text = data.get("text", "")
    await state.clear()
    ids = (await session.execute(select(User.tg_id))).scalars().all()
    await cb.message.edit_text(f"📤 Отправка {len(ids)} получателям...")
    sent = 0
    for tg_id in ids:
        try:
            await notifications.notify(tg_id, text)
            sent += 1
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(0.05)  # бережём лимиты Telegram
    await audit.log(session, "broadcast", actor_tg_id=cb.from_user.id,
                    actor_role="admin", details=f"sent={sent}/{len(ids)}")
    await session.commit()
    await cb.message.answer(
        f"✅ Рассылка завершена: {sent}/{len(ids)}.",
        reply_markup=keyboards.back_to_admin_kb(),
    )
    await cb.answer()


# ============================================================================
#  ФОТО услуг / мастеров
# ============================================================================

@router.callback_query(F.data.startswith("adm:svc:photo:"))
async def svc_photo_ask(cb: CallbackQuery, state: FSMContext, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await state.update_data(kind="svc", obj_id=int(cb.data.split(":")[3]))
    await state.set_state(AdminPhotoFlow.wait)
    await cb.message.edit_text("Пришлите фото услуги. Или отправьте «удалить», чтобы убрать фото.")
    await cb.answer()


@router.callback_query(F.data.startswith("adm:mst:photo:"))
async def mst_photo_ask(cb: CallbackQuery, state: FSMContext, role: Role) -> None:
    if not await _guard(cb, role):
        return
    await state.update_data(kind="mst", obj_id=int(cb.data.split(":")[3]))
    await state.set_state(AdminPhotoFlow.wait)
    await cb.message.edit_text("Пришлите фото мастера. Или отправьте «удалить», чтобы убрать фото.")
    await cb.answer()


async def _save_photo(state: FSMContext, session, file_id) -> str:
    data = await state.get_data()
    obj = await session.get(Service if data["kind"] == "svc" else Master, data["obj_id"])
    if obj:
        obj.photo_file_id = file_id
        await audit.log(session, "photo_set", actor_role="admin",
                        entity=data["kind"], entity_id=obj.id,
                        details="cleared" if file_id is None else "set")
        await session.commit()
    return data["kind"]


@router.message(AdminPhotoFlow.wait, F.photo)
async def photo_received(message: Message, state: FSMContext, session) -> None:
    file_id = message.photo[-1].file_id
    await _save_photo(state, session, file_id)
    await state.clear()
    await message.answer("✅ Фото сохранено.", reply_markup=keyboards.back_to_admin_kb())


@router.message(AdminPhotoFlow.wait, F.text)
async def photo_text(message: Message, state: FSMContext, session) -> None:
    if message.text.strip().lower() in ("удалить", "delete", "-"):
        await _save_photo(state, session, None)
        await state.clear()
        await message.answer("✅ Фото удалено.", reply_markup=keyboards.back_to_admin_kb())
    else:
        await message.answer("Пришлите фото или «удалить».")
