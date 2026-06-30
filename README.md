# ZapisBOT — self-hosted Telegram booking bot

White-label appointment booking system for service businesses (barbershops,
grooming, salons, dentists, massage, etc.) with a **full admin panel inside
Telegram** — no web panel required.

You run it yourself: drop in your bot token, edit `config.yaml`, start it.

> **Язык интерфейса бота — русский.** Все сообщения, кнопки и команды на русском.
> Этот README (для разработчика/продавца) — на английском.

---

## Features

- **Roles**: Client, Master, Admin (auto-resolved by Telegram id).
- **Client**: book a service → pick master → date → time slot; view, **reschedule** (limited) & cancel own bookings; service/master **photos** shown at confirmation.
- **Master**: see only their own daily schedule; mark *came* / *no-show*.
- **Admin panel** (`/admin`, inline buttons):
  - 📊 **Stats** — bookings per day/week/month, revenue, master load, cancellations, no-shows.
  - 📅 **Bookings** — view all (paginated) / today / by master; cancel, **reschedule**, complete, mark no-show.
  - 💈 **Services** — add / delete (with confirm + safety check) / enable / price / duration / **photo** / assign to masters.
  - 👨‍🔧 **Masters** — add / delete / working hours / **one-off days off** / **photo** / assign services.
  - 🚫 **Blacklist** — list, manual block/unblock.
  - ⚙️ **Settings** — reminders, buffer, cancel limits, no-show rules, retention. Values are **validated** (a bad number can't break the bot).
- **Automatic slots** — generated from working hours, service duration, buffer; overlaps prevented; min lead time.
- **Double-booking protection** — partial unique DB index (SQLite + PostgreSQL) blocks two active bookings on the same master/time even under races.
- **Notifications** — booking created / cancelled / **rescheduled**, reminders 24h & 1h before. Pluggable for SMS/WhatsApp.
- **Blacklist automation** — 2 no-shows → auto-block; cancellation-limit warnings.
- **Anti-flood** — per-user throttling middleware.
- **Phone capture** — optional “share contact” button stores the client's phone.
- **Immutable audit log** — every create/cancel/reschedule/status/admin action recorded. Enforced at the DB level on SQLite (triggers block UPDATE/DELETE), not just in code.
- **Data retention with stats archive** — completed/cancelled bookings purged after 7 days (configurable); no-shows kept longer. Daily totals (counts + revenue) are archived **before** purge, so statistics survive deletion.
- **Daily DB backup** — automatic SQLite snapshot (`backups/`) with rotation.
- **Master morning digest** — each master gets their day's bookings at 08:00 local.
- **CSV export** — admin exports all bookings as a CSV document in chat (Excel-ready).
- **Broadcast** — admin sends a message to all users from the panel.
- **About / contacts** — `/about` shows salon address, phone, map, socials (from config).
- **Anti-spam / anti-bot** — per-user throttling, max active bookings per client, booking cooldown, optional “share phone before booking”.
- **Hardening** — all user text HTML-escaped (no injection/markup breakage), input-length limits, global error handler.
- **SQLite** by default; switch to **PostgreSQL** by changing one line.
- **Long-polling** — single command, runs anywhere (no domain/HTTPS needed).

---

## Requirements

- **Python 3.10+ recommended** (works on 3.9; on 3.8 it auto-uses `backports.zoneinfo`).
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- Your Telegram numeric id (ask [@userinfobot](https://t.me/userinfobot)).

---

## Setup

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Create your config
cp config.example.yaml config.yaml      # Windows: copy config.example.yaml config.yaml

# 3. Edit config.yaml:
#    - bot_token   (or set the BOT_TOKEN env var)
#    - admins      (your Telegram id)
#    - business_name, timezone, services, masters

# 4. Run
python bot.py
```

On first run the database is created and your `services`, `masters`, and `rules`
from `config.yaml` are seeded. After that, manage everything from `/admin` —
the config seeds defaults only once (rules merge in new keys without overwriting
your edits).

---

## Configuration (`config.yaml`)

| Key | Meaning |
|-----|---------|
| `bot_token` | BotFather token (env `BOT_TOKEN` overrides). |
| `business_name` | Shown to clients. |
| `timezone` | IANA name, e.g. `Europe/Moscow`. |
| `currency`, `show_prices` | Money display. |
| `admins` | List of Telegram ids with admin access. |
| `database_url` | SQLite (default) or PostgreSQL DSN. |
| `services` | Seeded services (name/duration/price). |
| `masters` | Seeded staff (name, optional tg_id, working hours, services). |
| `rules` | Buffer, slot step, horizon, cancel/no-show rules, reminders, retention, anti-spam (`max_active_bookings`, `booking_cooldown_seconds`, `require_phone`). Editable later in `/admin → Settings`. |
| `contacts` | Salon address / phone / map / socials, shown in `/about`. |

See `config.example.yaml` for every field with comments.

## Tests

```bash
pip install -r requirements-dev.txt
pytest
```

Covers slot generation + buffer, double-booking prevention, auto-blacklist,
cancellation limits, and retention-archive correctness.

---

## Roles — how they're assigned

- **Admin**: any Telegram id listed in `admins:`.
- **Master**: any user whose id matches a master's `tg_id` (set it in config or in `/admin → Masters`).
- **Client**: everyone else.

---

## Switching to PostgreSQL

```bash
pip install asyncpg
```

```yaml
# config.yaml
database_url: "postgresql+asyncpg://user:password@localhost:5432/zapisbot"
```

Tables are created automatically on start. (For schema migrations over time,
add Alembic — the models live in `app/models.py`.)

---

## Project layout

```
bot.py                  entry point
config.example.yaml     template config
app/
  config.py             load/validate config.yaml
  models.py             SQLAlchemy ORM (SQLite/PostgreSQL)
  db.py                 engine, sessions, seeding, settings store
  slots.py              automatic slot generation + overlap/buffer logic
  booking_ops.py        create/cancel/reschedule/status (shared)
  blacklist.py          manual + auto blacklist, cancel limits
  audit.py              append-only audit logging
  notifications.py      Telegram notifier (SMS/WhatsApp-ready)
  scheduler.py          reminders + retention cleanup (APScheduler)
  middlewares.py        per-update DB session + role resolution
  keyboards.py          inline keyboards
  states.py             FSM state groups
  utils.py              timezone/format helpers
  handlers/
    common.py           /start /help /cancel
    client.py           booking flow + my bookings
    master.py           master schedule + status
    admin.py            full admin panel
```

---

## Extending

- **New notification channel**: implement `Notifier` in `app/notifications.py` and append it in `setup()`.
- **More admin reports**: add a callback in `app/handlers/admin.py` and a button in `keyboards.admin_main_kb()`.
- **Per-master one-off days off**: `Master.days_off` already holds a JSON list of ISO dates (`["2026-07-01"]`); slot generation respects it.

---

## Notes / limitations (MVP)

- FSM uses in-memory storage — a restart clears half-finished flows (not bookings).
- Single-process long-polling — ideal for one salon; runs on a cheap VPS or home PC.
- Reminders are checked once per minute; morning digest at 08:00, cleanup 03:00, backup 03:30 (local time).

---

## License / Лицензия

Licensed under the **PolyForm Noncommercial License 1.0.0** — see [LICENSE.md](LICENSE.md).

**Можно** (для некоммерческих целей): использовать, изменять, форкать, распространять.
**Нельзя**: использовать в коммерческих целях без отдельного разрешения автора.
**Обязательно**: сохранять указание автора (строку `Required Notice:` из LICENSE.md) во всех копиях и производных.

> ⚠️ Это «source-available», а не OSI open-source: лицензия запрещает коммерческое
> использование, поэтому GitHub пометит её как нестандартную/«Other». Это ожидаемо.

За коммерческой лицензией — свяжитесь с автором (контакт в `Required Notice`).
