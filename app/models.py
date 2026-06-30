"""SQLAlchemy ORM models. SQLite by default, PostgreSQL-compatible."""
from __future__ import annotations

import datetime as dt
import enum
from typing import List, Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Table,
    Column,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Role(str, enum.Enum):
    client = "client"
    master = "master"
    admin = "admin"


class BookingStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    completed = "completed"
    no_show = "no_show"
    cancelled = "cancelled"


# Many-to-many: which master can perform which service.
master_services = Table(
    "master_services",
    Base.metadata,
    Column("master_id", ForeignKey("masters.id", ondelete="CASCADE"), primary_key=True),
    Column("service_id", ForeignKey("services.id", ondelete="CASCADE"), primary_key=True),
)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[Optional[str]] = mapped_column(String(64))
    full_name: Mapped[Optional[str]] = mapped_column(String(256))
    phone: Mapped[Optional[str]] = mapped_column(String(32))
    role: Mapped[Role] = mapped_column(Enum(Role), default=Role.client)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )


class Service(Base):
    __tablename__ = "services"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    duration_min: Mapped[int] = mapped_column(Integer)
    price: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    photo_file_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    masters: Mapped[List["Master"]] = relationship(
        secondary=master_services, back_populates="services"
    )


class Master(Base):
    __tablename__ = "masters"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, index=True, nullable=True)
    # JSON-encoded dict: {"mon": "10:00-19:00", "tue": null, ...}
    working_hours: Mapped[Optional[str]] = mapped_column(Text)
    # JSON-encoded list of ISO dates ["2026-07-01", ...] for one-off days off.
    days_off: Mapped[Optional[str]] = mapped_column(Text, default="[]")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    photo_file_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    services: Mapped[List[Service]] = relationship(
        secondary=master_services, back_populates="masters"
    )


class Booking(Base):
    __tablename__ = "bookings"
    __table_args__ = (
        # Prevent double-booking the same master/time among ACTIVE bookings.
        # Partial unique index (cancelled/completed/no_show excluded so the
        # time can be reused later). Applied on both SQLite and PostgreSQL.
        Index(
            "uq_active_slot",
            "master_id",
            "start_time",
            unique=True,
            sqlite_where=text("status IN ('pending', 'confirmed')"),
            postgresql_where=text("status IN ('pending', 'confirmed')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    client_tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    client_name: Mapped[Optional[str]] = mapped_column(String(256))
    master_id: Mapped[int] = mapped_column(ForeignKey("masters.id"))
    service_id: Mapped[int] = mapped_column(ForeignKey("services.id"))
    # Stored as timezone-aware UTC datetimes.
    start_time: Mapped[dt.datetime] = mapped_column(DateTime, index=True)
    end_time: Mapped[dt.datetime] = mapped_column(DateTime)
    status: Mapped[BookingStatus] = mapped_column(
        Enum(BookingStatus), default=BookingStatus.confirmed, index=True
    )
    price: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )
    cancelled_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime, nullable=True)
    reminded_24h: Mapped[bool] = mapped_column(Boolean, default=False)
    reminded_1h: Mapped[bool] = mapped_column(Boolean, default=False)
    reschedule_count: Mapped[int] = mapped_column(Integer, default=0)

    master: Mapped[Master] = relationship()
    service: Mapped[Service] = relationship()


class Blacklist(Base):
    __tablename__ = "blacklist"
    __table_args__ = (UniqueConstraint("tg_id", name="uq_blacklist_tg"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, index=True)
    reason: Mapped[Optional[str]] = mapped_column(String(256))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLog(Base):
    """Append-only audit log. The application never updates or deletes rows here."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    timestamp: Mapped[dt.datetime] = mapped_column(
        DateTime, default=dt.datetime.utcnow, index=True
    )
    actor_tg_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    actor_role: Mapped[Optional[str]] = mapped_column(String(16))
    action: Mapped[str] = mapped_column(String(64), index=True)
    entity: Mapped[Optional[str]] = mapped_column(String(32))
    entity_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    details: Mapped[Optional[str]] = mapped_column(Text)


class Setting(Base):
    """Runtime-editable business rules (seeded from config.rules)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)


class DailyStat(Base):
    """Aggregated daily totals, written before old bookings are purged.

    Lets statistics survive data retention (revenue/counts are kept as numbers
    even after the underlying booking rows are deleted).
    """

    __tablename__ = "daily_stats"

    date: Mapped[str] = mapped_column(String(10), primary_key=True)  # local ISO date
    completed: Mapped[int] = mapped_column(Integer, default=0)
    cancelled: Mapped[int] = mapped_column(Integer, default=0)
    no_show: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[float] = mapped_column(Float, default=0.0)
