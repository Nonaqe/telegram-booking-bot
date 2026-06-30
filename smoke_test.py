"""Offline smoke test: config -> DB -> seed -> slots -> booking. No network."""
import asyncio
import datetime as dt
import os
import tempfile

import yaml

CFG = {
    "bot_token": "123456:DUMMYTOKEN",
    "business_name": "Test Shop",
    "timezone": "Europe/Moscow",
    "currency": "RUB",
    "show_prices": True,
    "admins": [1],
    "database_url": "sqlite+aiosqlite:///./_smoke.db",
    "services": [{"name": "Haircut", "duration": 45, "price": 1500}],
    "masters": [
        {
            "name": "Alex",
            "tg_id": 999,
            "working_hours": {
                "mon": "10:00-19:00", "tue": "10:00-19:00", "wed": "10:00-19:00",
                "thu": "10:00-19:00", "fri": "10:00-19:00", "sat": "10:00-19:00",
                "sun": "10:00-19:00",
            },
            "services": ["Haircut"],
        }
    ],
    "rules": {"min_lead_minutes": 0},
}


async def main() -> None:
    if os.path.exists("_smoke.db"):
        os.remove("_smoke.db")
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    yaml.safe_dump(CFG, tmp)
    tmp.close()
    os.environ["ZAPISBOT_CONFIG"] = tmp.name

    from app.config import load_config
    from app.db import create_all, init_engine, seed, get_sessionmaker
    from app.models import Master, Service
    from app.slots import available_slots
    from app import booking_ops
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    cfg = load_config()
    init_engine(cfg.database_url)
    await create_all()
    await seed(cfg)
    print("seed OK")

    sm = get_sessionmaker()
    async with sm() as s:
        svc = (await s.execute(select(Service))).scalars().all()
        mst = (await s.execute(
            select(Master).options(selectinload(Master.services))
        )).scalars().all()
        assert len(svc) == 1 and len(mst) == 1, "seed counts wrong"
        assert mst[0].services[0].name == "Haircut", "m2m link wrong"
        print(f"services={[x.name for x in svc]} masters={[m.name for m in mst]}")

        # Pick tomorrow (guaranteed working day, all days open).
        from app.utils import to_local, now_utc
        tomorrow = to_local(now_utc(), cfg.timezone).date() + dt.timedelta(days=1)
        slots = await available_slots(s, mst[0], svc[0], tomorrow, cfg.timezone)
        assert slots, "no slots generated"
        print(f"slots on {tomorrow}: {len(slots)} (first={slots[0]} last={slots[-1]})")

        first = slots[0]
        bk = await booking_ops.create_booking(
            s, client_tg_id=555, client_name="Bob",
            master=mst[0], service=svc[0], start_utc=first, tz=cfg.timezone,
        )
        print(f"booking created id={bk.id} status={bk.status.value} price={bk.price}")

        # Slot must now be gone (overlap + buffer).
        slots2 = await available_slots(s, mst[0], svc[0], tomorrow, cfg.timezone)
        assert first not in slots2, "booked slot still offered!"
        # Buffer (10m default) + 45m duration => fewer slots near booking.
        print(f"slots after booking: {len(slots2)} (was {len(slots)})")

        await booking_ops.cancel_booking(s, bk, actor_tg_id=555, actor_role="client")
        print(f"cancelled, status={bk.status.value}")

    from app.db import dispose
    await dispose()

    os.remove(tmp.name)
    if os.path.exists("_smoke.db"):
        os.remove("_smoke.db")
    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())
