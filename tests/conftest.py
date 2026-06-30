"""Pytest fixtures: fresh seeded DB per test."""
import datetime as dt
import os
import sys
import uuid

import pytest_asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.config import Config, ServiceCfg, MasterCfg, DEFAULT_RULES  # noqa: E402
from app import db as db_module  # noqa: E402


def make_config(**over) -> Config:
    rules = dict(DEFAULT_RULES)
    rules["min_lead_minutes"] = 0
    rules.update(over.pop("rules", {}))
    return Config(
        bot_token="123:DUMMY",
        business_name="Test",
        timezone="Europe/Moscow",
        currency="RUB",
        show_prices=True,
        admins=[1],
        database_url=f"sqlite+aiosqlite:///./_test_{uuid.uuid4().hex}.db",
        services=[ServiceCfg("Стрижка", 45, 1500)],
        masters=[
            MasterCfg(
                name="Алекс",
                tg_id=999,
                working_hours={k: "10:00-19:00" for k in
                               ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]},
                services=["Стрижка"],
            )
        ],
        rules=rules,
        **over,
    )


@pytest_asyncio.fixture
async def cfg_db():
    cfg = make_config()
    path = cfg.database_url.split("///")[1]
    db_module.init_engine(cfg.database_url)
    await db_module.create_all()
    await db_module.seed(cfg)
    try:
        yield cfg
    finally:
        await db_module.dispose()
        if os.path.exists(path):
            os.remove(path)


def tomorrow_local(cfg) -> dt.date:
    from app.utils import now_utc, to_local
    return to_local(now_utc(), cfg.timezone).date() + dt.timedelta(days=1)
