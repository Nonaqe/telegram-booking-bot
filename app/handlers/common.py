"""Общие команды: /start, /help, /cancel, главное меню, телефон."""
from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from sqlalchemy import select

from ..config import Config
from ..models import Role, User

router = Router()

# Тексты кнопок главного меню — используются для прерывания FSM-сценариев.
BTN_BOOK = "📅 Записаться"
BTN_MY = "📋 Мои записи"
BTN_ABOUT = "ℹ️ О нас"
BTN_SCHEDULE = "🗓 Моё расписание"
BTN_ADMIN = "🛠 Админка"
BTN_PHONE = "📱 Поделиться телефоном"

MENU_BUTTONS = {BTN_BOOK, BTN_MY, BTN_ABOUT, BTN_SCHEDULE, BTN_ADMIN, BTN_PHONE}

CONTACT_LABELS = {
    "address": "📍 Адрес",
    "phone": "📞 Телефон",
    "maps_url": "🗺 На карте",
    "instagram": "📷 Instagram",
    "telegram": "✈️ Telegram",
    "website": "🌐 Сайт",
}


def main_menu(role: Role) -> ReplyKeyboardMarkup:
    rows = [[KeyboardButton(text=BTN_BOOK), KeyboardButton(text=BTN_MY)]]
    rows.append([KeyboardButton(text=BTN_ABOUT)])
    if role == Role.master:
        rows.append([KeyboardButton(text=BTN_SCHEDULE)])
    if role == Role.admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    rows.append([KeyboardButton(text=BTN_PHONE, request_contact=True)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext, cfg: Config, role: Role) -> None:
    await state.clear()
    await message.answer(
        f"👋 Добро пожаловать в <b>{cfg.business_name}</b>!\n\n"
        "Через меню ниже можно записаться или посмотреть свои записи.",
        reply_markup=main_menu(role),
    )


@router.message(Command("help"))
async def cmd_help(message: Message, cfg: Config, role: Role) -> None:
    lines = [
        f"<b>{cfg.business_name}</b> — бот записи",
        "",
        "/book — записаться",
        "/my — мои записи",
    ]
    if role == Role.master:
        lines.append("/schedule — расписание на сегодня")
    if role == Role.admin:
        lines.append("/admin — панель администратора")
    await message.answer("\n".join(lines), reply_markup=main_menu(role))


@router.message(Command("about"))
@router.message(F.text == BTN_ABOUT)
async def cmd_about(message: Message, cfg: Config, role: Role) -> None:
    from ..utils import esc

    lines = [f"<b>{esc(cfg.business_name)}</b>", ""]
    if cfg.contacts:
        for key, label in CONTACT_LABELS.items():
            val = cfg.contacts.get(key)
            if val:
                if val.startswith("http"):
                    lines.append(f'{label}: <a href="{esc(val)}">ссылка</a>')
                else:
                    lines.append(f"{label}: {esc(val)}")
    else:
        lines.append("Контакты не указаны.")
    await message.answer("\n".join(lines), reply_markup=main_menu(role), disable_web_page_preview=True)


@router.message(Command("cancel"))
@router.message(F.text.casefold() == "отмена")
async def cmd_cancel(message: Message, state: FSMContext, role: Role) -> None:
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu(role))


@router.message(F.contact)
async def save_contact(message: Message, session, role: Role) -> None:
    """Сохранить номер телефона клиента (необязательно)."""
    contact = message.contact
    if contact.user_id and contact.user_id != message.from_user.id:
        await message.answer("Это не ваш контакт. Отправьте свой номер.")
        return
    user = (
        await session.execute(select(User).where(User.tg_id == message.from_user.id))
    ).scalar_one_or_none()
    if user:
        user.phone = contact.phone_number
        await session.commit()
    await message.answer("✅ Телефон сохранён.", reply_markup=main_menu(role))
