"""Explicit regional lookups; legacy callers work only with an unambiguous binding."""

from sqlalchemy import select

from .db import WalletAccount, WalletSession
from .domain import UserError


async def regional_row(session, model, user_id, region=None):
    if region is not None:
        return await session.get(model, (user_id, region))
    rows = list(await session.scalars(select(model).where(model.user_id == user_id).limit(2)))
    if len(rows) > 1:
        raise UserError("Обери регіон автобаю: DE, FR, IT або UK.")
    return rows[0] if rows else None


async def wallet_account(session, user_id, region=None):
    return await regional_row(session, WalletAccount, user_id, region)


async def wallet_session(session, user_id, region=None):
    return await regional_row(session, WalletSession, user_id, region)
