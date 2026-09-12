"""FastAPI dependencies shared across v1 route modules."""

from typing import Annotated

from fastapi import Depends
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import AuthUser, get_current_user
from app.db.session import get_db
from app.models import User


async def get_current_db_user(
    auth_user: Annotated[AuthUser, Depends(get_current_user)],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> User:
    """Resolve the verified Supabase identity to our internal `users` row,
    provisioning one on first sight — this is what makes anonymous/
    device-first sign-in work: the JWT `sub` is stable from the first
    request, so the local user row it maps to is created lazily rather
    than requiring a separate signup step.
    """
    existing = await db.execute(select(User).where(User.auth_provider_id == auth_user.id))
    user = existing.scalar_one_or_none()
    if user is not None:
        return user

    user = User(
        auth_provider_id=auth_user.id,
        email=auth_user.email,
        is_anonymous=auth_user.email is None,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        # Lost a race with a concurrent request for the same auth_user.id.
        await db.rollback()
        existing = await db.execute(select(User).where(User.auth_provider_id == auth_user.id))
        user = existing.scalar_one()
    else:
        await db.refresh(user)
    return user
