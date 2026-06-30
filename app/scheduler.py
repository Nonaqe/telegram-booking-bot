"""Background jobs: reminders, retention cleanup, master digest, DB backup."""
from __future__ import annotations

import datetime as dt
import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import delete, select, text
from sqlalchemy.orm import selectinload

from . import audit, notifications
from .config import Config
from .db import get_engine, get_setting, get_sessionmaker
from .models import Booking, BookingStatus, DailyStat, Master
from .tz import ZoneInfo
from .utils import esc, fmt_time, fmt_dt, local_to_utc_naive, make_local, now_utc, to_local

log = logging.getLogger(__name__)


async def _send_reminders(cfg: Config) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        do_24 = bool(await get_setting(session, "reminder_24h", True))
        do_1 = bool(await get_setting(session, "reminder_1h", True))
        now = now_utc()

        rows = (
            await session.execute(
                select(Booking).where(
                    Booking.status.in_(
                        [BookingStatus.pending, BookingStatus.confirmed]
                    ),
                    Booking.start_time > now,
                )
            )
        ).scalars().all()

        for b in rows:
            delta = b.start_time - now
            if do_24 and not b.reminded_24h and delta <= dt.timedelta(hours=24):
                await notifications.notify(
                    b.client_tg_id,
                    f"⏰ Напоминание: запись завтра в {fmt_dt(b.start_time, cfg.timezone)}.",
                )
                b.reminded_24h = True
            if do_1 and not b.reminded_1h and delta <= dt.timedelta(hours=1):
                await notifications.notify(
                    b.client_tg_id,
                    f"⏰ Напоминание: запись примерно через час ({fmt_dt(b.start_time, cfg.timezone)}).",
                )
                b.reminded_1h = True
        await session.commit()


async def _cleanup(cfg: Config) -> None:
    sm = get_sessionmaker()
    async with sm() as session:
        comp_days = int(await get_setting(session, "retention_completed_days", 7))
        canc_days = int(await get_setting(session, "retention_cancelled_days", 7))
        ns_days = int(await get_setting(session, "retention_no_show_days", 180))
        now = now_utc()

        async def _archive(row: Booking) -> None:
            """Fold a soon-to-be-deleted booking into the daily aggregate."""
            day = to_local(row.start_time, cfg.timezone).date().isoformat()
            stat = await session.get(DailyStat, day)
            if stat is None:
                stat = DailyStat(date=day)
                session.add(stat)
            if row.status == BookingStatus.completed:
                stat.completed = (stat.completed or 0) + 1
                stat.revenue = (stat.revenue or 0.0) + (row.price or 0.0)
            elif row.status == BookingStatus.cancelled:
                stat.cancelled = (stat.cancelled or 0) + 1
            elif row.status == BookingStatus.no_show:
                stat.no_show = (stat.no_show or 0) + 1

        async def purge(status: BookingStatus, days: int) -> int:
            if days <= 0:
                return 0
            cutoff = now - dt.timedelta(days=days)
            rows = (
                await session.execute(
                    select(Booking).where(
                        Booking.status == status, Booking.start_time < cutoff
                    )
                )
            ).scalars().all()
            for r in rows:
                await _archive(r)
            await session.flush()
            result = await session.execute(
                delete(Booking).where(
                    Booking.status == status, Booking.start_time < cutoff
                )
            )
            return result.rowcount or 0

        n = 0
        n += await purge(BookingStatus.completed, comp_days)
        n += await purge(BookingStatus.cancelled, canc_days)
        n += await purge(BookingStatus.no_show, ns_days)
        if n:
            await audit.log(
                session, "retention_cleanup", actor_role="system",
                details=f"purged {n} bookings",
            )
            log.info("Retention cleanup purged %d bookings", n)
        await session.commit()


async def _morning_digest(cfg: Config) -> None:
    """Утром каждому мастеру — список записей на сегодня."""
    sm = get_sessionmaker()
    async with sm() as session:
        today = to_local(now_utc(), cfg.timezone).date()
        day_start = local_to_utc_naive(make_local(today, dt.time.min, cfg.timezone))
        day_end = local_to_utc_naive(
            make_local(today + dt.timedelta(days=1), dt.time.min, cfg.timezone)
        )
        masters = (
            await session.execute(
                select(Master).where(Master.is_active.is_(True), Master.tg_id.is_not(None))
            )
        ).scalars().all()
        for m in masters:
            rows = (
                await session.execute(
                    select(Booking)
                    .where(
                        Booking.master_id == m.id,
                        Booking.start_time >= day_start,
                        Booking.start_time < day_end,
                        Booking.status.in_([BookingStatus.pending, BookingStatus.confirmed]),
                    )
                    .options(selectinload(Booking.service))
                    .order_by(Booking.start_time)
                )
            ).scalars().all()
            if not rows:
                continue
            lines = [f"☀️ Доброе утро! Записи на {today.strftime('%d.%m.%Y')}:", ""]
            for b in rows:
                lines.append(
                    f"🕐 {fmt_time(b.start_time, cfg.timezone)} — {esc(b.service.name)} "
                    f"({esc(b.client_name or b.client_tg_id)})"
                )
            await notifications.notify(m.tg_id, "\n".join(lines))


async def _backup(cfg: Config) -> None:
    """Ежедневный снимок БД (только SQLite) с ротацией."""
    if not cfg.database_url.startswith("sqlite"):
        return
    keep = 14
    os.makedirs("backups", exist_ok=True)
    stamp = dt.datetime.now().strftime("%Y%m%d")
    target = os.path.join("backups", f"zapisbot-{stamp}.db").replace("\\", "/")
    if os.path.exists(target):
        os.remove(target)
    engine = get_engine()
    # VACUUM нельзя выполнять внутри транзакции — нужен AUTOCOMMIT.
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="AUTOCOMMIT")
        await conn.execute(text(f"VACUUM INTO '{target}'"))
    log.info("Бэкап БД: %s", target)
    backups = sorted(
        f for f in os.listdir("backups")
        if f.startswith("zapisbot-") and f.endswith(".db")
    )
    for old in backups[:-keep]:
        try:
            os.remove(os.path.join("backups", old))
        except OSError:
            pass


def setup_scheduler(cfg: Config) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler(timezone="UTC")
    local_tz = ZoneInfo(cfg.timezone)
    scheduler.add_job(_send_reminders, "interval", minutes=1, args=[cfg], id="reminders")
    scheduler.add_job(_cleanup, "cron", hour=3, minute=0, args=[cfg], id="cleanup")
    scheduler.add_job(
        _morning_digest, "cron", hour=8, minute=0,
        args=[cfg], id="digest", timezone=local_tz,
    )
    scheduler.add_job(
        _backup, "cron", hour=3, minute=30, args=[cfg], id="backup", timezone=local_tz,
    )
    return scheduler
