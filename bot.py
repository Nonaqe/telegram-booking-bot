"""Точка входа ZapisBOT. Запуск: python bot.py"""
from __future__ import annotations

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent

from app import notifications
from app.config import Config, load_config
from app.db import create_all, dispose, init_engine, seed
from app.handlers import build_router
from app.middlewares import ServicesMiddleware, ThrottlingMiddleware
from app.scheduler import setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("zapisbot")


async def set_commands(bot: Bot) -> None:
    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Старт / меню"),
            BotCommand(command="book", description="Записаться"),
            BotCommand(command="my", description="Мои записи"),
            BotCommand(command="about", description="О нас / контакты"),
            BotCommand(command="schedule", description="Мастер: расписание на сегодня"),
            BotCommand(command="admin", description="Админ-панель"),
            BotCommand(command="help", description="Помощь"),
        ]
    )


def build_dispatcher(cfg: Config) -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    throttle = ThrottlingMiddleware()
    services = ServicesMiddleware(cfg)
    for observer in (dp.message, dp.callback_query):
        observer.middleware(throttle)
        observer.middleware(services)
    dp.include_router(build_router())

    @dp.errors()
    async def on_error(event: ErrorEvent) -> bool:
        log.exception("Необработанная ошибка: %s", event.exception)
        upd = event.update
        try:
            if upd.callback_query:
                await upd.callback_query.answer("Произошла ошибка, попробуйте позже.", show_alert=True)
            elif upd.message:
                await upd.message.answer("Произошла ошибка, попробуйте позже.")
        except Exception:  # noqa: BLE001 - не даём ошибке нотификации всплыть
            pass
        return True

    return dp


async def main() -> None:
    cfg = load_config()
    log.info("Старт ZapisBOT для «%s» (tz=%s)", cfg.business_name, cfg.timezone)

    init_engine(cfg.database_url)
    await create_all()
    await seed(cfg)

    bot = Bot(
        token=cfg.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    notifications.setup(bot)
    dp = build_dispatcher(cfg)

    scheduler = setup_scheduler(cfg)
    scheduler.start()
    await set_commands(bot)

    await bot.delete_webhook(drop_pending_updates=False)
    log.info("Запуск в режиме polling...")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        await dispose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        log.info("Остановлено.")
