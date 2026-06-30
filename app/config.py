"""Load and validate config.yaml into a typed structure."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

CONFIG_PATH = Path(os.getenv("ZAPISBOT_CONFIG", "config.yaml"))


@dataclass
class ServiceCfg:
    name: str
    duration: int
    price: float


@dataclass
class MasterCfg:
    name: str
    tg_id: Optional[int]
    working_hours: Dict[str, Optional[str]]
    services: List[str]


@dataclass
class Config:
    bot_token: str
    business_name: str
    timezone: str
    currency: str
    show_prices: bool
    admins: List[int]
    database_url: str
    services: List[ServiceCfg]
    masters: List[MasterCfg]
    rules: Dict[str, Any] = field(default_factory=dict)
    # Контакты салона (показываются в разделе «О нас»).
    contacts: Dict[str, str] = field(default_factory=dict)


# Settings whose value must be an integer / boolean. Used to validate admin
# edits so a bad value can never reach the slot engine.
INT_SETTINGS = {
    "buffer_minutes",
    "slot_step_minutes",
    "booking_horizon_days",
    "min_lead_minutes",
    "cancel_min_hours",
    "cancel_limit_count",
    "cancel_limit_window_days",
    "no_show_blacklist_threshold",
    "retention_completed_days",
    "retention_cancelled_days",
    "retention_no_show_days",
    "max_active_bookings",
    "booking_cooldown_seconds",
    "max_client_reschedules",
}
BOOL_SETTINGS = {"reminder_24h", "reminder_1h", "require_phone"}


def coerce_setting(key: str, text: str):
    """Validate+convert a setting value. Raises ValueError on bad input."""
    t = (text or "").strip()
    if key in BOOL_SETTINGS:
        low = t.lower()
        if low in ("true", "1", "да", "yes", "on"):
            return True
        if low in ("false", "0", "нет", "no", "off"):
            return False
        raise ValueError("Нужно да/нет (true/false).")
    if key in INT_SETTINGS:
        try:
            val = int(t)
        except ValueError:
            raise ValueError("Нужно целое число.")
        if val < 0:
            raise ValueError("Число не может быть отрицательным.")
        return val
    return t


DEFAULT_RULES: Dict[str, Any] = {
    "buffer_minutes": 10,
    "slot_step_minutes": 15,
    "booking_horizon_days": 14,
    "min_lead_minutes": 60,
    "cancel_min_hours": 3,
    "cancel_limit_count": 3,
    "cancel_limit_window_days": 30,
    "no_show_blacklist_threshold": 2,
    "reminder_24h": True,
    "reminder_1h": True,
    "retention_completed_days": 7,
    "retention_cancelled_days": 7,
    "retention_no_show_days": 180,
    # Anti-spam / anti-bot.
    "max_active_bookings": 3,        # макс. предстоящих записей на клиента
    "booking_cooldown_seconds": 0,   # мин. секунд между созданием записей
    "require_phone": False,          # требовать телефон до записи
    "max_client_reschedules": 2,     # макс. переносов записи клиентом
}


def load_config(path: Path = CONFIG_PATH) -> Config:
    if not path.exists():
        raise FileNotFoundError(
            f"Config file not found: {path}. "
            "Copy config.example.yaml to config.yaml and edit it."
        )
    with path.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    token = os.getenv("BOT_TOKEN") or raw.get("bot_token", "")
    if not token or "PUT-YOUR" in token:
        raise ValueError(
            "bot_token is not set. Put it in config.yaml or set BOT_TOKEN env var."
        )

    admins = [int(x) for x in (raw.get("admins") or [])]
    if not admins:
        raise ValueError("At least one admin id must be set in config.yaml (admins:).")

    services = [
        ServiceCfg(
            name=str(s["name"]),
            duration=int(s["duration"]),
            price=float(s.get("price", 0) or 0),
        )
        for s in (raw.get("services") or [])
    ]

    masters = [
        MasterCfg(
            name=str(m["name"]),
            tg_id=int(m["tg_id"]) if m.get("tg_id") else None,
            working_hours=m.get("working_hours") or {},
            services=list(m.get("services") or []),
        )
        for m in (raw.get("masters") or [])
    ]

    rules = dict(DEFAULT_RULES)
    rules.update(raw.get("rules") or {})

    contacts = {str(k): str(v) for k, v in (raw.get("contacts") or {}).items() if v}

    return Config(
        bot_token=token,
        business_name=str(raw.get("business_name", "My Business")),
        timezone=str(raw.get("timezone", "UTC")),
        currency=str(raw.get("currency", "")),
        show_prices=bool(raw.get("show_prices", True)),
        admins=admins,
        database_url=str(raw.get("database_url", "sqlite+aiosqlite:///./zapisbot.db")),
        services=services,
        masters=masters,
        rules=rules,
        contacts=contacts,
    )
