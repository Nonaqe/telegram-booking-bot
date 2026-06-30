"""Append-only audit logging. Rows are never updated or deleted by the app."""
from __future__ import annotations

from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .models import AuditLog


async def log(
    session: AsyncSession,
    action: str,
    *,
    actor_tg_id: Optional[int] = None,
    actor_role: Optional[str] = None,
    entity: Optional[str] = None,
    entity_id: Optional[int] = None,
    details: Optional[str] = None,
) -> None:
    """Record an immutable audit entry. Caller is responsible for commit."""
    session.add(
        AuditLog(
            action=action,
            actor_tg_id=actor_tg_id,
            actor_role=actor_role,
            entity=entity,
            entity_id=entity_id,
            details=details,
        )
    )
