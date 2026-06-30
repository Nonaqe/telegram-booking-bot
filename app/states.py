"""FSM state groups for multi-step flows."""
from aiogram.fsm.state import State, StatesGroup


class BookingFlow(StatesGroup):
    service = State()
    master = State()
    date = State()
    slot = State()
    confirm = State()


class AdminServiceFlow(StatesGroup):
    name = State()
    duration = State()
    price = State()
    edit_value = State()


class AdminMasterFlow(StatesGroup):
    name = State()
    tg_id = State()
    hours = State()


class AdminBlacklistFlow(StatesGroup):
    tg_id = State()
    reason = State()


class AdminSettingFlow(StatesGroup):
    value = State()


class AdminRescheduleFlow(StatesGroup):
    date = State()
    slot = State()


class AdminDayOffFlow(StatesGroup):
    date = State()


class AdminBroadcastFlow(StatesGroup):
    text = State()
    confirm = State()


class AdminPhotoFlow(StatesGroup):
    wait = State()


class ClientRescheduleFlow(StatesGroup):
    date = State()
    slot = State()
