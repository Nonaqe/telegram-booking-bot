"""Notification dispatch. Telegram now; architecture allows SMS/WhatsApp later.

To add a channel, implement a Notifier and register it in NOTIFIERS.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send(self, recipient: int, text: str) -> None: ...


class TelegramNotifier:
    def __init__(self, bot: Bot) -> None:
        self.bot = bot

    async def send(self, recipient: int, text: str) -> None:
        try:
            await self.bot.send_message(recipient, text)
        except TelegramAPIError as e:
            log.warning("Failed to notify %s: %s", recipient, e)


# Future channels (SMS/WhatsApp) plug in here.
NOTIFIERS: List[Notifier] = []


def setup(bot: Bot) -> None:
    NOTIFIERS.clear()
    NOTIFIERS.append(TelegramNotifier(bot))


async def notify(recipient: Optional[int], text: str) -> None:
    if not recipient:
        return
    for n in NOTIFIERS:
        await n.send(recipient, text)


async def notify_admins(admins: List[int], text: str) -> None:
    for admin_id in admins:
        await notify(admin_id, text)
