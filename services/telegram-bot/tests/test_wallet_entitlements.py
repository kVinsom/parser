from datetime import timedelta

import pytest
from test_wallet import setup

from vpars_bot.db import Subscription, User, WalletOrder, utcnow
from vpars_bot.wallet import WalletError


async def lite(sessions):
    async with sessions.begin() as session:
        sub = await session.get(Subscription, 101)
        sub.level = "LITE"
        sub.period_started_at = utcnow()
        (await session.get(User, 101)).enabled_sources = ["vinted_de"]


async def test_success_reserves_monthly_quota_and_blocks_second_item(db, registry):
    _, sessions = db
    service, site, worker = await setup(sessions, registry)
    await lite(sessions)
    order = await service.request(101, 10)
    await worker.once()
    await service.decide(101, order.id, pay=True)
    await worker.once()
    async with sessions() as session:
        saved = await session.get(WalletOrder, order.id)
        assert saved.state == "succeeded" and saved.quota_period_start is not None
    assert site.payments == 1

    async def another(*args):
        return {
            "item_url": "https://www.vinted.de/items/124-shirt",
            "price": {"amount": "10.00", "currency": "EUR"},
        }

    service.resolver = another
    with pytest.raises(WalletError, match="SubscriptionAutobuyLimit"):
        await service.request(101, 11)
    assert site.payments == 1


@pytest.mark.parametrize("change", ["expires", "quota", "region"])
async def test_permission_is_rechecked_after_checkout_before_debit(db, registry, change):
    _, sessions = db
    service, site, worker = await setup(sessions, registry)
    await lite(sessions)
    order = await service.request(101, 10)
    await worker.once()
    await service.decide(101, order.id, pay=True)
    checkout = site.checkout

    async def changed(order):
        quote = await checkout(order)
        async with sessions.begin() as session:
            sub = await session.get(Subscription, 101)
            if change == "expires":
                sub.expires_at = utcnow() - timedelta(seconds=1)
            elif change == "region":
                (await session.get(User, 101)).enabled_sources = ["vinted_fr"]
            else:
                session.add(
                    WalletOrder(
                        id="other",
                        user_id=101,
                        item_id="999",
                        url="https://www.vinted.de/items/999",
                        listed_price=100,
                        currency="EUR",
                        state="unknown",
                        quota_period_start=sub.period_started_at,
                    )
                )
        return quote

    site.checkout = changed
    await worker.once()
    assert site.payments == 0
    async with sessions() as session:
        saved = await session.get(WalletOrder, order.id)
        assert (
            saved.state == "failed"
            and saved.error
            == {
                "expires": "SubscriptionRequired",
                "quota": "SubscriptionAutobuyLimit",
                "region": "SubscriptionRegionLimit",
            }[change]
        )
