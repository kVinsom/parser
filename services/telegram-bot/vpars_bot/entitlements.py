"""One server-side policy for catalog, worker, delivery and purchase eligibility."""

from dataclasses import dataclass
from datetime import UTC, timedelta
from uuid import uuid4

from sqlalchemy import and_, func, or_, select

from .db import ControlCommand, Search, SearchRun, Subscription, WalletOrder, utcnow
from .domain import UserError


@dataclass(frozen=True)
class Plan:
    level: str
    filters: int
    regions: int | None
    exclusions: bool
    autobuy: int | None
    vinted_only: bool = False


PLANS = {
    "LITE": Plan("LITE", 3, 1, False, 1, True),
    "PRO": Plan("PRO", 6, 3, True, 5),
    "ULTRA": Plan("ULTRA", 10, None, True, None),
}
NO_PLAN = Plan("Без підписки", 0, 0, False, 0)
MESSAGES = {
    "SubscriptionRequired": "Потрібна активна підписка. Відкрий «💳 Купити підписку».",
    "SubscriptionFilterLimit": "Цей фільтр понад ліміт тарифу. Прибери зайві фільтри або зміни тариф.",
    "SubscriptionRegionLimit": "Цей регіон не входить у вибір за тарифом. Зміни вибір у «Маркетплейси».",
    "SubscriptionExclusions": "Виключення слів доступне в PRO та ULTRA. Прибери виключення або зміни тариф.",
    "SubscriptionMarketplace": "Тариф LITE підтримує тільки Vinted.",
    "SubscriptionAutobuyLimit": "Ліміт викупів Autobuy за тарифом вичерпано.",
}


async def plan_for(session, user):
    if user is None:
        return NO_PLAN
    if user.is_admin:
        return PLANS["ULTRA"]
    subscription = await session.get(Subscription, user.id)
    if subscription and subscription.expires_at.replace(tzinfo=UTC) > utcnow():
        return PLANS.get(subscription.level, NO_PLAN)
    return NO_PLAN


def configured_sources(user, registry):
    return set(registry.sources).intersection(
        registry.sources if user.enabled_sources is None else user.enabled_sources
    )


def allowed_sources(user, registry, plan):
    selected = configured_sources(user, registry)
    result, regions = set(), set()
    for key, source in registry.sources.items():
        if key not in selected or (plan.vinted_only and source.provider != "vinted"):
            continue
        # Several regional entries for the same marketplace share one quota.
        identities = {(source.provider, source.aliases.get(r, r)) for r in source.regions}
        if plan.regions is not None and len(regions | identities) > plan.regions:
            continue
        regions |= identities
        result.add(key)
    return result


async def search_ids(session, user_id):
    return list(
        await session.scalars(
            select(Search.id)
            .where(Search.user_id == user_id, Search.archived.is_(False))
            .order_by(Search.created_at, Search.id)
        )
    )


async def access_error(session, user, search, registry, source_id=None):
    plan = await plan_for(session, user)
    if not plan.filters:
        return "SubscriptionRequired"
    if search.id not in (await search_ids(session, user.id))[: plan.filters]:
        return "SubscriptionFilterLimit"
    if search.filters.get("excluded_keywords") and not plan.exclusions:
        return "SubscriptionExclusions"
    if source_id is not None:
        source = registry.sources.get(source_id)
        if source and plan.vinted_only and source.provider != "vinted":
            return "SubscriptionMarketplace"
        if source_id not in allowed_sources(user, registry, plan):
            return "SubscriptionRegionLimit"
    return None


async def validate_save(session, user, filters, registry, *, adding):
    plan = await plan_for(session, user)
    if not plan.filters:
        raise UserError(MESSAGES["SubscriptionRequired"])
    if adding and len(await search_ids(session, user.id)) >= plan.filters:
        raise UserError(
            f"{plan.level}: можна зберегти до {plan.filters} фільтрів. Прибери зайвий або зміни тариф."
        )
    if filters.excluded_keywords and not plan.exclusions:
        raise UserError(MESSAGES["SubscriptionExclusions"])
    chosen = allowed_sources(user, registry, plan)
    if not filters.follow_enabled_sources and set(filters.sources) - chosen:
        raise UserError(MESSAGES["SubscriptionRegionLimit"])


async def reconcile_user(session, user, registry):
    """Invalidate disallowed runs, preserving saved filters and requiring a new Start."""
    rows = (
        await session.execute(
            select(Search, SearchRun)
            .join(SearchRun)
            .where(
                Search.user_id == user.id,
                Search.archived.is_(False),
                SearchRun.state.in_(["requested", "active", "demo"]),
            )
        )
    ).all()
    changed = 0
    for search, run in rows:
        error = await access_error(session, user, search, registry, run.source_id)
        if error is None:
            continue
        run.generation += 1
        run.state = "paused_limit"
        run.active_until = None
        run.last_error = error
        session.add(
            ControlCommand(
                id=str(uuid4()),
                user_id=user.id,
                kind="invalidate",
                payload={
                    "search_id": search.id,
                    "delivery_epoch": user.delivery_epoch,
                    "filter_version": search.version,
                    "generations": {run.source_id: run.generation},
                },
            )
        )
        changed += 1
    return changed


async def autobuy_access(session, user, *, exclude_order=None):
    """Caller holds the user lock; unresolved debits consume quota until reconciled."""
    plan = await plan_for(session, user)
    if not plan.filters:
        return "SubscriptionRequired", None
    if user.is_admin:
        return None, None
    subscription = await session.get(Subscription, user.id)
    anchor = subscription.period_started_at.replace(tzinfo=UTC)
    now = utcnow()
    period = anchor + timedelta(days=30 * max(0, (now - anchor).days // 30))
    used = await session.scalar(
        select(func.count())
        .select_from(WalletOrder)
        .where(
            WalletOrder.user_id == user.id,
            WalletOrder.id != exclude_order if exclude_order else True,
            WalletOrder.state.in_(["paying", "succeeded", "unknown"]),
            or_(
                WalletOrder.quota_period_start == period,
                and_(WalletOrder.quota_period_start.is_(None), WalletOrder.updated_at >= period),
            ),
        )
    )
    if plan.autobuy is not None and used >= plan.autobuy:
        return "SubscriptionAutobuyLimit", period
    return None, period
