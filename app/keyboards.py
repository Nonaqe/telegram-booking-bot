"""Inline keyboard builders (RU). Callback format: 'domain:action:arg'."""
from __future__ import annotations

import datetime as dt
from typing import Iterable, Optional, Sequence, Tuple

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from .models import Booking, Master, Service
from .utils import WEEKDAY_NAMES, money, to_local


def _kb(rows: Iterable[Sequence[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])


# ---- Client booking flow ----------------------------------------------------

def services_kb(services: Sequence[Service], show_prices: bool, currency: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in services:
        label = s.name
        if show_prices and s.price:
            label += f" · {money(s.price, currency)}"
        label += f" · {s.duration_min}мин"
        b.button(text=label, callback_data=f"book:svc:{s.id}")
    b.button(text="« Отмена", callback_data="book:cancel:0")
    b.adjust(1)
    return b.as_markup()


def masters_kb(masters: Sequence[Master]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for m in masters:
        b.button(text=m.name, callback_data=f"book:mst:{m.id}")
    b.button(text="« Назад", callback_data="book:back:svc")
    b.adjust(1)
    return b.as_markup()


def dates_kb(dates: Sequence[dt.date], prefix: str = "book", back: str = "book:back:mst") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for d in dates:
        label = f"{WEEKDAY_NAMES[d.weekday()]} {d.strftime('%d.%m')}"
        b.button(text=label, callback_data=f"{prefix}:day:{d.isoformat()}")
    b.button(text="« Назад", callback_data=back)
    b.adjust(3)
    return b.as_markup()


def slots_kb(slots: Sequence[dt.datetime], tz: str, prefix: str = "book", back: str = "book:back:day") -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in slots:
        # s is naive UTC; encode ISO so decoding is unambiguous.
        b.button(text=to_local(s, tz).strftime("%H:%M"), callback_data=f"{prefix}:slot:{s.isoformat()}")
    b.button(text="« Назад", callback_data=back)
    b.adjust(4)
    return b.as_markup()


def confirm_kb() -> InlineKeyboardMarkup:
    return _kb([
        [InlineKeyboardButton(text="✅ Подтвердить", callback_data="book:confirm:1")],
        [InlineKeyboardButton(text="« Отмена", callback_data="book:cancel:0")],
    ])


def my_bookings_kb(bookings: Sequence[Booking], tz: str) -> InlineKeyboardMarkup:
    rows = []
    for bk in bookings:
        when = to_local(bk.start_time, tz).strftime("%d.%m %H:%M")
        rows.append([
            InlineKeyboardButton(text=f"🔁 Перенести {when}", callback_data=f"my:resch:{bk.id}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"my:cancel:{bk.id}"),
        ])
    return _kb(rows)


# ---- Master panel -----------------------------------------------------------

def master_booking_kb(bk: Booking) -> InlineKeyboardMarkup:
    return _kb([
        [
            InlineKeyboardButton(text="✅ Пришёл", callback_data=f"mst:done:{bk.id}"),
            InlineKeyboardButton(text="🚫 Не пришёл", callback_data=f"mst:noshow:{bk.id}"),
        ],
    ])


# ---- Admin panel ------------------------------------------------------------

def admin_main_kb() -> InlineKeyboardMarkup:
    return _kb([
        [
            InlineKeyboardButton(text="📊 Статистика", callback_data="adm:stats:menu"),
            InlineKeyboardButton(text="📅 Записи", callback_data="adm:bk:menu"),
        ],
        [
            InlineKeyboardButton(text="💈 Услуги", callback_data="adm:svc:list"),
            InlineKeyboardButton(text="👨‍🔧 Мастера", callback_data="adm:mst:list"),
        ],
        [
            InlineKeyboardButton(text="🚫 Чёрный список", callback_data="adm:bl:list"),
            InlineKeyboardButton(text="⚙️ Настройки", callback_data="adm:set:list"),
        ],
        [
            InlineKeyboardButton(text="📢 Рассылка", callback_data="adm:bc:start"),
        ],
    ])


def back_to_admin_kb() -> InlineKeyboardMarkup:
    return _kb([[InlineKeyboardButton(text="« В админку", callback_data="adm:home:0")]])


def confirm_delete_kb(entity: str, entity_id: int, back: str) -> InlineKeyboardMarkup:
    return _kb([
        [InlineKeyboardButton(text="✅ Да, удалить", callback_data=f"adm:{entity}:delyes:{entity_id}")],
        [InlineKeyboardButton(text="« Отмена", callback_data=back)],
    ])


def stats_menu_kb() -> InlineKeyboardMarkup:
    return _kb([
        [
            InlineKeyboardButton(text="День", callback_data="adm:stats:day"),
            InlineKeyboardButton(text="Неделя", callback_data="adm:stats:week"),
            InlineKeyboardButton(text="Месяц", callback_data="adm:stats:month"),
        ],
        [InlineKeyboardButton(text="« В админку", callback_data="adm:home:0")],
    ])


def bookings_menu_kb() -> InlineKeyboardMarkup:
    return _kb([
        [InlineKeyboardButton(text="📋 Все предстоящие", callback_data="adm:bk:all:0")],
        [InlineKeyboardButton(text="👨‍🔧 По мастеру", callback_data="adm:bk:bymaster")],
        [InlineKeyboardButton(text="📆 Сегодня", callback_data="adm:bk:today")],
        [InlineKeyboardButton(text="📤 Экспорт CSV", callback_data="adm:bk:export")],
        [InlineKeyboardButton(text="« В админку", callback_data="adm:home:0")],
    ])


def broadcast_confirm_kb() -> InlineKeyboardMarkup:
    return _kb([
        [InlineKeyboardButton(text="✅ Отправить всем", callback_data="adm:bc:send")],
        [InlineKeyboardButton(text="« Отмена", callback_data="adm:home:0")],
    ])


def pager_kb(cb_prefix: str, page: int, has_next: bool) -> InlineKeyboardMarkup:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="« Назад", callback_data=f"{cb_prefix}:{page - 1}"))
    if has_next:
        row.append(InlineKeyboardButton(text="Вперёд »", callback_data=f"{cb_prefix}:{page + 1}"))
    rows = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton(text="« В админку", callback_data="adm:home:0")])
    return _kb(rows)


def admin_booking_actions_kb(bk: Booking) -> InlineKeyboardMarkup:
    return _kb([
        [
            InlineKeyboardButton(text="❌ Отменить", callback_data=f"adm:bk:cancel:{bk.id}"),
            InlineKeyboardButton(text="🔁 Перенести", callback_data=f"adm:bk:resch:{bk.id}"),
        ],
        [
            InlineKeyboardButton(text="✅ Выполнена", callback_data=f"adm:bk:done:{bk.id}"),
            InlineKeyboardButton(text="🚫 Не пришёл", callback_data=f"adm:bk:noshow:{bk.id}"),
        ],
    ])


def admin_masters_pick_kb(masters: Sequence[Master], action: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for m in masters:
        b.button(text=m.name, callback_data=f"adm:{action}:{m.id}")
    b.button(text="« В админку", callback_data="adm:home:0")
    b.adjust(2)
    return b.as_markup()


def admin_services_list_kb(services: Sequence[Service], currency: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for s in services:
        status = "" if s.is_active else " (выкл)"
        b.button(
            text=f"{s.name} · {s.duration_min}мин · {money(s.price, currency)}{status}",
            callback_data=f"adm:svc:view:{s.id}",
        )
    b.button(text="➕ Добавить услугу", callback_data="adm:svc:add")
    b.button(text="« В админку", callback_data="adm:home:0")
    b.adjust(1)
    return b.as_markup()


def admin_service_view_kb(s: Service) -> InlineKeyboardMarkup:
    toggle = "Выключить" if s.is_active else "Включить"
    return _kb([
        [
            InlineKeyboardButton(text="💲 Цена", callback_data=f"adm:svc:price:{s.id}"),
            InlineKeyboardButton(text="⏱ Длительность", callback_data=f"adm:svc:dur:{s.id}"),
        ],
        [
            InlineKeyboardButton(text="🖼 Фото", callback_data=f"adm:svc:photo:{s.id}"),
            InlineKeyboardButton(text=f"🔁 {toggle}", callback_data=f"adm:svc:toggle:{s.id}"),
        ],
        [
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm:svc:del:{s.id}"),
            InlineKeyboardButton(text="« Услуги", callback_data="adm:svc:list"),
        ],
    ])


def admin_masters_list_kb(masters: Sequence[Master]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for m in masters:
        status = "" if m.is_active else " (выкл)"
        b.button(text=f"{m.name}{status}", callback_data=f"adm:mst:view:{m.id}")
    b.button(text="➕ Добавить мастера", callback_data="adm:mst:add")
    b.button(text="« В админку", callback_data="adm:home:0")
    b.adjust(1)
    return b.as_markup()


def admin_master_view_kb(m: Master) -> InlineKeyboardMarkup:
    toggle = "Выключить" if m.is_active else "Включить"
    return _kb([
        [
            InlineKeyboardButton(text="🕐 Часы", callback_data=f"adm:mst:hours:{m.id}"),
            InlineKeyboardButton(text="💈 Услуги", callback_data=f"adm:mst:svc:{m.id}"),
        ],
        [
            InlineKeyboardButton(text="🚫 Выходные", callback_data=f"adm:mst:dayoff:{m.id}"),
            InlineKeyboardButton(text="🖼 Фото", callback_data=f"adm:mst:photo:{m.id}"),
        ],
        [
            InlineKeyboardButton(text=f"🔁 {toggle}", callback_data=f"adm:mst:toggle:{m.id}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"adm:mst:del:{m.id}"),
        ],
        [InlineKeyboardButton(text="« Мастера", callback_data="adm:mst:list")],
    ])


def admin_master_services_kb(m: Master, all_services: Sequence[Service]) -> InlineKeyboardMarkup:
    assigned = {s.id for s in m.services}
    b = InlineKeyboardBuilder()
    for s in all_services:
        mark = "✅" if s.id in assigned else "▫️"
        b.button(text=f"{mark} {s.name}", callback_data=f"adm:mst:svctoggle:{m.id}:{s.id}")
    b.button(text="« Мастер", callback_data=f"adm:mst:view:{m.id}")
    b.adjust(1)
    return b.as_markup()


def admin_dayoff_kb(m: Master, days: Sequence[str]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for d in days:
        b.button(text=f"❌ {d}", callback_data=f"adm:mst:dayoffdel:{m.id}:{d}")
    b.button(text="➕ Добавить выходной", callback_data=f"adm:mst:dayoffadd:{m.id}")
    b.button(text="« Мастер", callback_data=f"adm:mst:view:{m.id}")
    b.adjust(1)
    return b.as_markup()


def admin_blacklist_kb(rows: Sequence[Tuple[int, Optional[str]]]) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for tg_id, _reason in rows:
        b.button(text=f"✅ Разблокировать {tg_id}", callback_data=f"adm:bl:unblock:{tg_id}")
    b.button(text="➕ Заблокировать по ID", callback_data="adm:bl:add")
    b.button(text="« В админку", callback_data="adm:home:0")
    b.adjust(1)
    return b.as_markup()


def admin_settings_kb(settings: dict) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for key in sorted(settings.keys()):
        b.button(text=f"{key} = {settings[key]}", callback_data=f"adm:set:edit:{key}")
    b.button(text="« В админку", callback_data="adm:home:0")
    b.adjust(1)
    return b.as_markup()
