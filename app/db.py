"""Database engine, session factory, and first-run seeding."""
from __future__ import annotations

import json
from typing import Any, Optional

from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from .config import Config
from .models import Base, Master, Service, Setting

_engine: Optional[AsyncEngine] = None
_sessionmaker: Optional[async_sessionmaker[AsyncSession]] = None


def init_engine(database_url: str) -> AsyncEngine:
    global _engine, _sessionmaker
    _engine = create_async_engine(database_url, echo=False, future=True)
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)

    # SQLite ignores foreign keys unless turned on per connection.
    if database_url.startswith("sqlite"):
        @event.listens_for(_engine.sync_engine, "connect")
        def _fk_on(dbapi_conn, _rec):  # pragma: no cover
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    return _engine


def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("DB not initialized. Call init_engine() first.")
    return _sessionmaker


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("DB not initialized. Call init_engine() first.")
    return _engine


async def dispose() -> None:
    if _engine is not None:
        await _engine.dispose()


_AUDIT_TRIGGERS_SQLITE = [
    "CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log is immutable'); END;",
    "CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log "
    "BEGIN SELECT RAISE(ABORT, 'audit_log is immutable'); END;",
]


# Колонки, добавленные после v1 — дотягиваем существующие SQLite-базы.
_SQLITE_ADD_COLUMNS = [
    ("services", "photo_file_id", "TEXT"),
    ("masters", "photo_file_id", "TEXT"),
    ("bookings", "reschedule_count", "INTEGER DEFAULT 0"),
]


async def create_all() -> None:
    assert _engine is not None
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if _engine.dialect.name == "sqlite":
            from sqlalchemy import text as _text
            # Делаем audit_log по-настоящему неизменяемым на уровне БД.
            for ddl in _AUDIT_TRIGGERS_SQLITE:
                await conn.execute(_text(ddl))
            # Простейшая миграция: добавляем недостающие колонки.
            for table, col, typ in _SQLITE_ADD_COLUMNS:
                rows = await conn.execute(_text(f"PRAGMA table_info({table})"))
                existing = {r[1] for r in rows}
                if col not in existing:
                    await conn.execute(
                        _text(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                    )


async def seed(cfg: Config) -> None:
    """Insert services, masters and settings on first run (idempotent-ish)."""
    sm = get_sessionmaker()
    async with sm() as session:
        # Settings: insert only missing keys (preserve admin edits).
        existing_keys = set(
            (await session.execute(select(Setting.key))).scalars().all()
        )
        for key, value in cfg.rules.items():
            if key not in existing_keys:
                session.add(Setting(key=key, value=json.dumps(value)))

        # Services: seed only if table empty.
        svc_count = len((await session.execute(select(Service.id))).scalars().all())
        name_to_service: dict[str, Service] = {}
        if svc_count == 0:
            for s in cfg.services:
                obj = Service(name=s.name, duration_min=s.duration, price=s.price)
                session.add(obj)
                name_to_service[s.name] = obj
            await session.flush()

        # Masters: seed only if table empty.
        master_count = len((await session.execute(select(Master.id))).scalars().all())
        if master_count == 0:
            # Need service lookup by name (handles both fresh and pre-seeded).
            if not name_to_service:
                for s in (await session.execute(select(Service))).scalars().all():
                    name_to_service[s.name] = s
            for m in cfg.masters:
                master = Master(
                    name=m.name,
                    tg_id=m.tg_id,
                    working_hours=json.dumps(m.working_hours),
                    days_off="[]",
                )
                master.services = [
                    name_to_service[n] for n in m.services if n in name_to_service
                ]
                session.add(master)

        await session.commit()


# ---- Settings helpers -------------------------------------------------------

async def get_setting(session: AsyncSession, key: str, default: Any = None) -> Any:
    row = await session.get(Setting, key)
    if row is None:
        return default
    try:
        return json.loads(row.value)
    except (json.JSONDecodeError, TypeError):
        return row.value


async def set_setting(session: AsyncSession, key: str, value: Any) -> None:
    row = await session.get(Setting, key)
    payload = json.dumps(value)
    if row is None:
        session.add(Setting(key=key, value=payload))
    else:
        row.value = payload


async def all_settings(session: AsyncSession) -> dict[str, Any]:
    rows = (await session.execute(select(Setting))).scalars().all()
    out: dict[str, Any] = {}
    for r in rows:
        try:
            out[r.key] = json.loads(r.value)
        except (json.JSONDecodeError, TypeError):
            out[r.key] = r.value
    return out
