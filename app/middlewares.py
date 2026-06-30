"""Middlewares: anti-flood, DB session per update, user upsert, role resolution."""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, User as TgUser
from sqlalchemy import select

from .config import Config
from .db import get_sessionmaker
from .models import Master, Role, User


class ThrottlingMiddleware(BaseMiddleware):
    """Drop updates that arrive faster than `rate` seconds per user."""

    def __init__(self, rate: float = 0.4) -> None:
        self.rate = rate
        self._last: Dict[int, float] = {}

    async def __call__(self, handler, event: TelegramObject, data: Dict[str, Any]) -> Any:
        user: TgUser | None = data.get("event_from_user")
        if user is not None:
            now = time.monotonic()
            last = self._last.get(user.id, 0.0)
            if now - last < self.rate:
                if isinstance(event, CallbackQuery):
                    await event.answer()  # silently ack
                return None
            self._last[user.id] = now
            # Keep the dict from growing unbounded.
            if len(self._last) > 10000:
                self._last.clear()
        return await handler(event, data)


class ServicesMiddleware(BaseMiddleware):
    """Opens a session, upserts the user, resolves role, injects into data."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        sm = get_sessionmaker()
        async with sm() as session:
            data["session"] = session
            data["cfg"] = self.cfg

            if tg_user is not None:
                role = await self._resolve(session, tg_user)
                data["role"] = role
            result = await handler(event, data)
            return result

    async def _resolve(self, session, tg_user: TgUser) -> Role:
        # Determine role first.
        if tg_user.id in self.cfg.admins:
            role = Role.admin
        else:
            master = (
                await session.execute(
                    select(Master).where(Master.tg_id == tg_user.id)
                )
            ).scalar_one_or_none()
            role = Role.master if master else Role.client

        # Upsert user record.
        user = (
            await session.execute(select(User).where(User.tg_id == tg_user.id))
        ).scalar_one_or_none()
        full_name = tg_user.full_name
        if user is None:
            session.add(
                User(
                    tg_id=tg_user.id,
                    username=tg_user.username,
                    full_name=full_name,
                    role=role,
                )
            )
        else:
            user.username = tg_user.username
            user.full_name = full_name
            user.role = role
        await session.commit()
        return role
