"""Core logic tests: slots, double-booking, blacklist, retention archive."""
import datetime as dt

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app import booking_ops, blacklist
from app.db import get_sessionmaker
from app.models import Booking, BookingStatus, DailyStat, Master, Service
from app.slots import available_slots
from conftest import tomorrow_local


async def _fixtures(session):
    svc = (await session.execute(select(Service))).scalars().first()
    mst = (await session.execute(
        select(Master).options(selectinload(Master.services))
    )).scalars().first()
    return svc, mst


async def test_seed(cfg_db):
    async with get_sessionmaker()() as s:
        svc, mst = await _fixtures(s)
        assert svc.name == "Стрижка"
        assert mst.services[0].id == svc.id


async def test_slots_and_buffer(cfg_db):
    async with get_sessionmaker()() as s:
        svc, mst = await _fixtures(s)
        day = tomorrow_local(cfg_db)
        slots = await available_slots(s, mst, svc, day, cfg_db.timezone)
        assert slots, "должны быть слоты"
        n0 = len(slots)
        await booking_ops.create_booking(
            s, client_tg_id=5, client_name="Боб", master=mst, service=svc,
            start_utc=slots[0], tz=cfg_db.timezone,
        )
        slots2 = await available_slots(s, mst, svc, day, cfg_db.timezone)
        assert slots[0] not in slots2, "занятый слот всё ещё предлагается"
        # 45мин услуга + 10мин буфер при шаге 15мин убирает несколько слотов.
        assert len(slots2) < n0


async def test_double_booking_blocked(cfg_db):
    async with get_sessionmaker()() as s:
        svc, mst = await _fixtures(s)
        day = tomorrow_local(cfg_db)
        slots = await available_slots(s, mst, svc, day, cfg_db.timezone)
        await booking_ops.create_booking(
            s, client_tg_id=5, client_name="A", master=mst, service=svc,
            start_utc=slots[0], tz=cfg_db.timezone,
        )
    # Второй клиент на то же время — должен получить отказ.
    async with get_sessionmaker()() as s2:
        svc, mst = await _fixtures(s2)
        with pytest.raises(booking_ops.BookingError):
            await booking_ops.create_booking(
                s2, client_tg_id=6, client_name="B", master=mst, service=svc,
                start_utc=slots[0], tz=cfg_db.timezone,
            )


async def test_auto_blacklist_on_no_shows(cfg_db):
    async with get_sessionmaker()() as s:
        svc, mst = await _fixtures(s)
        # Порог по умолчанию = 2 неявки.
        for i in range(2):
            bk = Booking(
                client_tg_id=77, client_name="NS", master_id=mst.id, service_id=svc.id,
                start_time=dt.datetime(2026, 1, 1, 10 + i), end_time=dt.datetime(2026, 1, 1, 11 + i),
                status=BookingStatus.confirmed, price=svc.price,
            )
            s.add(bk)
            await s.flush()
            blacklisted = await booking_ops.set_status(
                s, bk, BookingStatus.no_show, actor_tg_id=1, actor_role="master"
            )
        assert blacklisted is True
        assert await blacklist.is_blacklisted(s, 77) is not None


async def test_cancel_limit(cfg_db):
    async with get_sessionmaker()() as s:
        svc, mst = await _fixtures(s)
        for i in range(3):
            bk = Booking(
                client_tg_id=88, client_name="C", master_id=mst.id, service_id=svc.id,
                start_time=dt.datetime(2026, 2, 1, 10 + i), end_time=dt.datetime(2026, 2, 1, 11 + i),
                status=BookingStatus.confirmed, price=svc.price,
            )
            s.add(bk)
            await s.flush()
            await booking_ops.cancel_booking(s, bk, actor_tg_id=88, actor_role="client")
        assert await blacklist.is_cancel_restricted(s, 88) is True


async def test_max_active_bookings(cfg_db):
    from app.db import set_setting
    async with get_sessionmaker()() as s:
        await set_setting(s, "max_active_bookings", 1)
        await s.commit()
        svc, mst = await _fixtures(s)
        day = tomorrow_local(cfg_db)
        slots = await available_slots(s, mst, svc, day, cfg_db.timezone)
        await booking_ops.create_booking(
            s, client_tg_id=42, client_name="A", master=mst, service=svc,
            start_utc=slots[0], tz=cfg_db.timezone,
        )
        with pytest.raises(booking_ops.BookingError):
            await booking_ops.create_booking(
                s, client_tg_id=42, client_name="A", master=mst, service=svc,
                start_utc=slots[3], tz=cfg_db.timezone,
            )


async def test_audit_immutable(cfg_db):
    from sqlalchemy import text
    from app import audit
    async with get_sessionmaker()() as s:
        await audit.log(s, "test_action", actor_tg_id=1, actor_role="admin")
        await s.commit()
        with pytest.raises(Exception):
            await s.execute(text("UPDATE audit_log SET action='hacked'"))
            await s.commit()
    async with get_sessionmaker()() as s:
        with pytest.raises(Exception):
            await s.execute(text("DELETE FROM audit_log"))
            await s.commit()


async def test_reschedule_counts_and_photo_column(cfg_db):
    async with get_sessionmaker()() as s:
        svc, mst = await _fixtures(s)
        # photo-колонка существует и пишется
        svc.photo_file_id = "AgACfake"
        await s.commit()
        day = tomorrow_local(cfg_db)
        slots = await available_slots(s, mst, svc, day, cfg_db.timezone)
        bk = await booking_ops.create_booking(
            s, client_tg_id=11, client_name="R", master=mst, service=svc,
            start_utc=slots[0], tz=cfg_db.timezone,
        )
        assert bk.reschedule_count == 0
        await booking_ops.reschedule_booking(
            s, bk, mst, svc, slots[4],
            actor_tg_id=11, actor_role="client", tz=cfg_db.timezone,
        )
        assert bk.reschedule_count == 1
        refreshed = await s.get(Service, svc.id)
        assert refreshed.photo_file_id == "AgACfake"


async def test_backup_creates_file(cfg_db):
    import os
    from app.scheduler import _backup
    await _backup(cfg_db)
    files = [f for f in os.listdir("backups") if f.startswith("zapisbot-")] \
        if os.path.exists("backups") else []
    assert files, "бэкап не создан"


async def test_retention_archives_stats(cfg_db):
    from app.scheduler import _cleanup
    from app.utils import to_local

    async with get_sessionmaker()() as s:
        svc, mst = await _fixtures(s)
        old = dt.datetime.utcnow() - dt.timedelta(days=30)
        bk = Booking(
            client_tg_id=9, client_name="Old", master_id=mst.id, service_id=svc.id,
            start_time=old, end_time=old + dt.timedelta(minutes=45),
            status=BookingStatus.completed, price=1500,
        )
        s.add(bk)
        await s.commit()

    await _cleanup(cfg_db)

    async with get_sessionmaker()() as s:
        remaining = (await s.execute(select(Booking))).scalars().all()
        assert remaining == [], "старая запись должна быть удалена"
        day = to_local(old, cfg_db.timezone).date().isoformat()
        stat = await s.get(DailyStat, day)
        assert stat is not None and stat.completed == 1 and stat.revenue == 1500
