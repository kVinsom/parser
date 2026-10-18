import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from vpars_bot.application import AdminService, Catalog
from vpars_bot.db import Notification, Operation, RoleEvent, Subscription, User
from vpars_bot.domain import UserError
from vpars_bot.subscriptions import SubscriptionService, profile_text


async def setup(catalog, profiles):
    await catalog.register(101, 101, "support", "Support", 1, start=True)
    await catalog.register(202, 202, "bob", "Bob", 2, start=True)
    profiles.names.update({101: "support", 202: "bob"})
    async with catalog.sessions.begin() as session:
        (await session.get(User, 101)).is_admin = True


async def test_grant_replay_extension_and_profile_expiration(services, profiles, monkeypatch):
    catalog, _, admins = services
    await setup(catalog, profiles)
    service = SubscriptionService(catalog.sessions, admins)
    now = datetime(2026, 9, 27, 10, 0, tzinfo=UTC)
    monkeypatch.setattr("vpars_bot.subscriptions.utcnow", lambda: now)
    result = await service.grant(101, 202, "bob", "PRO", 30, "first")
    saved = await service.get(202)
    assert saved.level == "PRO" and saved.granted_by == 101
    assert saved.expires_at.replace(tzinfo=UTC) == now + timedelta(days=30)
    assert "PRO" in profile_text(saved) and "до 27.10.2026 12:00" in profile_text(saved)
    now += timedelta(days=1)
    assert await service.grant(101, 202, "bob", "PRO", 30, "first") == result
    with pytest.raises(UserError, match="застаріла"):
        await service.grant(101, 202, "bob", "ULTRA", 10, "first")
    await service.grant(101, 202, "bob", "LITE", 2, "second")
    saved = await service.get(202)
    assert saved.level == "LITE" and saved.expires_at.replace(tzinfo=UTC) == now + timedelta(
        days=31
    )
    now += timedelta(days=31)
    assert "неактивний" in profile_text(saved) and "LITE" in profile_text(saved)
    assert "неактивний" in profile_text(None) and "Ultra" not in profile_text(None)
    async with catalog.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Operation)) == 2


@pytest.mark.parametrize("days", [0, -3651, 3651, 1.5, True])
async def test_invalid_days_cannot_grant(services, profiles, days):
    catalog, _, admins = services
    await setup(catalog, profiles)
    service = SubscriptionService(catalog.sessions, admins)
    with pytest.raises(UserError, match="кількість днів"):
        await service.grant(101, 202, "bob", "PRO", days, "bad")
    assert await service.get(202) is None


async def test_subtract_replay_and_overdraft_expire_without_negative_balance(
    services, profiles, monkeypatch
):
    catalog, _, admins = services
    await setup(catalog, profiles)
    service = SubscriptionService(catalog.sessions, admins)
    now = datetime(2026, 10, 3, 10, tzinfo=UTC)
    monkeypatch.setattr("vpars_bot.subscriptions.utcnow", lambda: now)
    await service.grant(101, 202, "bob", "PRO", 10, "initial")
    result = await service.grant(101, 202, "bob", "PRO", -3, "subtract")
    assert datetime.fromisoformat(result["expires_at"]) == now + timedelta(days=7)
    assert await service.grant(101, 202, "bob", "PRO", -3, "subtract") == result
    with pytest.raises(UserError, match="застаріла"):
        await service.grant(101, 202, "bob", "PRO", -4, "subtract")
    result = await service.grant(101, 202, "bob", "PRO", -30, "finish")
    assert datetime.fromisoformat(result["expires_at"]) == now and not result["active"]
    assert "неактивний" in profile_text(await service.get(202))
    async with catalog.sessions() as session:
        notes = list(await session.scalars(select(Notification).order_by(Notification.created_at)))
        assert len(notes) == 3
        assert "скорочено" in notes[1].payload["text"] and "-3" in notes[1].payload["text"]
        assert "завершено" in notes[2].payload["text"]
    with pytest.raises(UserError, match="немає активної"):
        await service.grant(101, 202, "bob", "PRO", -1, "expired")
    now += timedelta(days=5)
    renewed = await service.grant(101, 202, "bob", "LITE", 2, "renewed")
    assert datetime.fromisoformat(renewed["expires_at"]) == now + timedelta(days=2)


async def test_cannot_subtract_from_missing_subscription(services, profiles):
    catalog, _, admins = services
    await setup(catalog, profiles)
    service = SubscriptionService(catalog.sessions, admins)
    with pytest.raises(UserError, match="немає активної"):
        await service.grant(101, 202, "bob", "PRO", -5, "missing")
    assert await service.get(202) is None
    async with catalog.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Notification)) == 0


async def test_grant_checks_authority_and_recipient_again(services, profiles, monkeypatch):
    catalog, _, admins = services
    await setup(catalog, profiles)
    service = SubscriptionService(catalog.sessions, admins)
    with pytest.raises(UserError, match="адміністратору"):
        await service.grant(202, 202, "bob", "PRO", 30, "forged")
    with pytest.raises(UserError, match="Обери"):
        await service.grant(101, 202, "bob", "FAKE", 30, "invalid")
    profiles.names[202] = "renamed"
    with pytest.raises(UserError, match="перевірити"):
        await service.grant(101, 202, "bob", "PRO", 30, "stale")

    async def revoke_during_verification(*args):
        async with catalog.sessions.begin() as session:
            (await session.get(User, 101)).is_admin = False

    monkeypatch.setattr(profiles, "verify", revoke_during_verification)
    with pytest.raises(UserError, match="знято"):
        await service.grant(101, 202, "bob", "PRO", 30, "revoked")
    assert await service.get(202) is None


async def test_support_admin_binds_verified_id_once(services, profiles):
    catalog, _, admins = services
    await catalog.register(101, 101, "BladeBotSupport", "Support", 1, start=True)
    with pytest.raises(UserError):
        await admins.claim_support_role(101, "BladeBotSupport")
    assert not (await catalog.user(101)).is_admin
    profiles.names[101] = "bladebotsupport"
    await admins.claim_support_role(101, "BladeBotSupport")
    await admins.claim_support_role(101, "BladeBotSupport")
    assert (await catalog.user(101)).is_admin
    # Recycled username cannot acquire a second admin identity, even after revocation.
    await catalog.register(101, 101, "renamed", "Support", 2)
    await catalog.register(202, 202, "BladeBotSupport", "Other", 3, start=True)
    profiles.names[202] = "bladebotsupport"
    async with catalog.sessions.begin() as session:
        (await session.get(User, 101)).is_admin = False
    await admins.claim_support_role(202, "BladeBotSupport")
    assert not (await catalog.user(202)).is_admin
    async with catalog.sessions() as session:
        assert await session.scalar(select(func.count()).select_from(RoleEvent)) == 1


@pytest.mark.integration
async def test_postgres_migration_and_concurrent_retry_only_grant_once(pg_db, registry, profiles):
    _, sessions = pg_db
    catalog = Catalog(sessions, registry)
    await setup(catalog, profiles)
    service = SubscriptionService(sessions, AdminService(sessions, profiles))
    results = await asyncio.gather(
        *[service.grant(101, 202, "bob", "ULTRA", 7, "same-operation") for _ in range(2)]
    )
    assert results[0] == results[1]
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Subscription)) == 1
        assert await session.scalar(select(func.count()).select_from(Operation)) == 1


@pytest.mark.integration
async def test_postgres_concurrent_adjustments_and_replay_keep_the_balance(
    pg_db, registry, profiles, monkeypatch
):
    _, sessions = pg_db
    catalog = Catalog(sessions, registry)
    await setup(catalog, profiles)
    service = SubscriptionService(sessions, AdminService(sessions, profiles))
    now = datetime(2026, 10, 3, 10, tzinfo=UTC)
    monkeypatch.setattr("vpars_bot.subscriptions.utcnow", lambda: now)
    await service.grant(101, 202, "bob", "PRO", 20, "initial")
    await asyncio.gather(
        service.grant(101, 202, "bob", "PRO", 5, "add"),
        service.grant(101, 202, "bob", "PRO", -3, "subtract"),
        service.grant(101, 202, "bob", "PRO", -3, "subtract"),
    )
    saved = await service.get(202)
    assert saved.expires_at == now + timedelta(days=22)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(Operation)) == 3
        assert await session.scalar(select(func.count()).select_from(Notification)) == 3
