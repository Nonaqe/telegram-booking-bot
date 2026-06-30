"""Handler routers, registered in dependency order."""
from aiogram import Router

from . import admin, client, common, master


def build_router() -> Router:
    root = Router()
    root.include_router(common.router)
    root.include_router(admin.router)
    root.include_router(master.router)
    root.include_router(client.router)
    return root
