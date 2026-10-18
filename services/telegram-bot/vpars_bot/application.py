from datetime import UTC, timedelta
from typing import Protocol
from uuid import uuid4

from sqlalchemy import func, select, update

from .db import (
    ControlCommand,
    Guard,
    Operation,
    RoleEvent,
    Search,
    SearchRun,
    SearchVersion,
    User,
    utcnow,
)
from .domain import Filters, SourceRegistry, UserError, normalize_username
from .entitlements import (
    MESSAGES,
    access_error,
    allowed_sources,
    plan_for,
    reconcile_user,
    validate_save,
)


class ProfileVerifier(Protocol):
    async def verify(self, user_id: int, username: str | None) -> None: ...


async def locked_user(session, user_id: int) -> User:
    user = await session.scalar(select(User).where(User.id == user_id).with_for_update())
    if user is None:
        raise UserError("Спочатку виконай /start.")
    return user


async def owned_search(session, user_id, search_id) -> Search:
    search = await session.scalar(
        select(Search).where(Search.id == search_id, Search.user_id == user_id)
    )
    if search is None:
        raise UserError("Предмет не знайдено у твоєму списку.")
    return search


async def previous(session, user_id, key, kind):
    operation = await session.get(Operation, (user_id, key))
    if operation and operation.kind != kind:
        raise UserError("Ця кнопка застаріла. Відкрий меню знову.")
    return operation.result if operation else None


def remember(session, user_id, key, kind, result):
    session.add(Operation(user_id=user_id, key=key, kind=kind, result=result))


def command(session, user_id, kind, payload):
    session.add(ControlCommand(id=str(uuid4()), user_id=user_id, kind=kind, payload=payload))


async def invalidate(session, user, search):
    runs = (await session.scalars(select(SearchRun).where(SearchRun.search_id == search.id))).all()
    for run in runs:
        run.generation += 1
        run.state = "stopped"
        run.active_until = None
    command(
        session,
        user.id,
        "invalidate",
        {
            "search_id": search.id,
            "delivery_epoch": user.delivery_epoch,
            "filter_version": search.version,
            "generations": {r.source_id: r.generation for r in runs},
        },
    )


def selected_sources(user, registry):
    return set(registry.sources).intersection(
        registry.sources if user.enabled_sources is None else user.enabled_sources
    )


class Catalog:
    def __init__(self, sessions, registry: SourceRegistry):
        self.sessions = sessions
        self.registry = registry

    async def register(self, user_id, chat_id, username, full_name, update_id, *, start=False):
        if user_id != chat_id:
            raise UserError("Напиши боту в особисті повідомлення.")
        # The common unchanged-profile path needs one atomic UPDATE and no global profile lock.
        async with self.sessions.begin() as session:
            user = await session.scalar(
                update(User)
                .where(
                    User.id == user_id,
                    User.chat_id == chat_id,
                    User.username == username,
                    User.full_name == full_name[:256],
                    User.profile_update_id < update_id,
                )
                .values(profile_update_id=update_id, profile_seen_at=utcnow())
                .returning(User)
            )
            if user is not None:
                return user
        async with self.sessions.begin() as session:
            await session.scalar(select(Guard).where(Guard.name == "profiles").with_for_update())
            user = await session.get(User, user_id)
            if user is None:
                if not start:
                    raise UserError("Спочатку виконай /start.")
                user = User(
                    id=user_id, chat_id=chat_id, full_name=full_name[:256], profile_update_id=-1
                )
                session.add(user)
            seen = user.profile_seen_at
            old = seen is not None and seen.replace(tzinfo=UTC) < utcnow() - timedelta(days=7)
            if update_id > user.profile_update_id or old:
                key = normalize_username(username) if username else None
                if key:
                    conflict = await session.scalar(
                        select(User).where(User.username_key == key, User.id != user_id)
                    )
                    if conflict:
                        if conflict.profile_update_id > update_id and not old:
                            key = None
                        else:
                            conflict.username_key = None
                            conflict.username = None
                            await session.flush()
                user.username = username if key else None
                user.username_key = key
                user.full_name = full_name[:256]
                user.profile_update_id = update_id
                user.profile_seen_at = utcnow()
            await session.flush()
            return user

    async def user(self, user_id):
        async with self.sessions() as session:
            user = await session.get(User, user_id)
            if user is None:
                raise UserError("Спочатку виконай /start.")
            return user

    async def get(self, user_id, search_id):
        async with self.sessions() as session:
            return await owned_search(session, user_id, search_id)

    async def marketplaces(self, user_id):
        async with self.sessions() as session:
            user = await session.get(User, user_id)
            return allowed_sources(user, self.registry, await plan_for(session, user))

    async def plan(self, user_id):
        async with self.sessions() as session:
            return await plan_for(session, await session.get(User, user_id))

    async def toggle_marketplace(self, user_id, source_id, key):
        if source_id not in self.registry.sources:
            raise UserError("Цей маркетплейс зараз недоступний.")
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            replay = await previous(session, user_id, key, "marketplace")
            if replay:
                return replay["enabled"]
            plan = await plan_for(session, user)
            chosen = allowed_sources(user, self.registry, plan)
            enabled = source_id not in chosen
            if enabled:
                if not plan.filters:
                    raise UserError(MESSAGES["SubscriptionRequired"])
                if plan.vinted_only and self.registry.sources[source_id].provider != "vinted":
                    raise UserError(MESSAGES["SubscriptionMarketplace"])
                chosen.add(source_id)
                # Validate the proposed selection before persisting it.
                old_selection = user.enabled_sources
                user.enabled_sources = sorted(chosen)
                permitted = allowed_sources(user, self.registry, plan)
                user.enabled_sources = old_selection
                if chosen != permitted:
                    raise UserError(
                        f"{plan.level}: до {plan.regions} регіонів. Спочатку вимкни один із вибраних."
                    )
            else:
                chosen.remove(source_id)
                runs = (
                    await session.scalars(
                        select(SearchRun)
                        .join(Search)
                        .where(Search.user_id == user_id, SearchRun.source_id == source_id)
                    )
                ).all()
                for run in runs:
                    run.generation += 1
                    run.state = "stopped"
                    run.active_until = None
                    item = await session.get(Search, run.search_id)
                    command(
                        session,
                        user_id,
                        "invalidate",
                        {
                            "search_id": item.id,
                            "delivery_epoch": user.delivery_epoch,
                            "filter_version": item.version,
                            "generations": {source_id: run.generation},
                        },
                    )
            user.enabled_sources = sorted(chosen)
            remember(session, user_id, key, "marketplace", {"enabled": enabled})
            return enabled

    async def toggle_filter(self, user_id, search_id, key):
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            replay = await previous(session, user_id, key, "filter_selection")
            if replay:
                return replay["enabled"]
            item = await owned_search(session, user_id, search_id)
            if item.archived:
                raise UserError("Цей фільтр уже прибрано. Відкрий список знову.")
            if not item.enabled:
                error = await access_error(session, user, item, self.registry)
                if error:
                    raise UserError(MESSAGES[error])
            item.enabled = not item.enabled
            if not item.enabled:
                await invalidate(session, user, item)
            remember(session, user_id, key, "filter_selection", {"enabled": item.enabled})
            return item.enabled

    async def count(self, user_id, *, enabled_only=False):
        async with self.sessions() as session:
            query = (
                select(func.count())
                .select_from(Search)
                .where(Search.user_id == user_id, Search.archived.is_(False))
            )
            if enabled_only:
                query = query.where(Search.enabled.is_(True))
            return await session.scalar(query)

    async def list(self, user_id, page=0, archived=False, page_size=6):
        async with self.sessions() as session:
            query = select(Search).where(Search.user_id == user_id, Search.archived == archived)
            count = await session.scalar(select(func.count()).select_from(query.subquery()))
            page = max(0, min(page, max(0, (count - 1) // page_size)))
            items = (
                await session.scalars(
                    query.order_by(Search.created_at, Search.id)
                    .offset(page * page_size)
                    .limit(page_size)
                )
            ).all()
            return items, count, page

    async def save(self, user_id, filters: Filters, key, *, search_id=None, version=None):
        filters = self.registry.validate(filters)
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            replay = await previous(session, user_id, key, "save")
            if replay:
                return await owned_search(session, user_id, replay["search_id"])
            await validate_save(session, user, filters, self.registry, adding=not search_id)
            if filters.follow_enabled_sources:
                permitted = allowed_sources(user, self.registry, await plan_for(session, user))
                sources = {
                    key: source.regions
                    for key, source in self.registry.sources.items()
                    if key in permitted
                }
                filters = Filters.model_validate({**filters.model_dump(), "sources": sources})
                for source_id in sources:
                    self.registry.for_source(filters, source_id)
            issues = await self.registry.issues(filters)
            if issues:
                raise UserError("\n".join(issues))
            if search_id:
                item = await owned_search(session, user_id, search_id)
                if item.version != version or item.archived:
                    raise UserError("Картка вже змінилася. Відкрий її та повтори редагування.")
                item.version += 1
                item.filters = filters.model_dump(mode="json")
                await invalidate(session, user, item)
            else:
                item = Search(
                    id=str(uuid4()),
                    user_id=user_id,
                    filters=filters.model_dump(mode="json"),
                    version=1,
                )
                session.add(item)
                await session.flush()
            session.add(
                SearchVersion(search_id=item.id, version=item.version, filters=item.filters)
            )
            for source_id in filters.sources:
                if await session.get(SearchRun, (item.id, source_id)) is None:
                    session.add(SearchRun(search_id=item.id, source_id=source_id))
            remember(session, user_id, key, "save", {"search_id": item.id})
            await session.flush()
            return item

    async def archive(self, user_id, search_id, archived, version, key):
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            if await previous(session, user_id, key, "archive"):
                return
            item = await owned_search(session, user_id, search_id)
            if item.version != version:
                raise UserError("Картка вже змінилася. Відкрий її знову.")
            if not archived:
                filters = self.registry.validate(Filters.model_validate(item.filters))
                await validate_save(session, user, filters, self.registry, adding=item.archived)
            if item.archived != archived:
                item.archived = archived
                item.version += 1
                session.add(
                    SearchVersion(search_id=item.id, version=item.version, filters=item.filters)
                )
                await invalidate(session, user, item)
            remember(session, user_id, key, "archive", {"done": True})


class AdminService:
    SUPPORT_USERNAME = "bladebotsupport"
    SUPPORT_ROLE_KEY = "designated-admin:bladebotsupport"

    def __init__(self, sessions, verifier: ProfileVerifier):
        self.sessions = sessions
        self.verifier = verifier

    async def require(self, user_id):
        async with self.sessions() as session:
            user = await session.get(User, user_id)
            if user is None or not user.is_admin:
                raise UserError("Ця дія доступна лише адміністратору.")

    async def claim_support_role(self, user_id, username):
        """Bind the owner-designated support account once, using a verified numeric ID."""
        if not username or normalize_username(username) != self.SUPPORT_USERNAME:
            return
        async with self.sessions() as session:
            if await session.get(RoleEvent, self.SUPPORT_ROLE_KEY):
                return
        await self.verifier.verify(user_id, self.SUPPORT_USERNAME)
        async with self.sessions.begin() as session:
            await session.scalar(select(Guard).where(Guard.name == "admins").with_for_update())
            if await session.get(RoleEvent, self.SUPPORT_ROLE_KEY):
                return
            recipient = await locked_user(session, user_id)
            if recipient.username_key != self.SUPPORT_USERNAME:
                raise UserError("Нік змінився. Повтори /start.")
            session.add(
                RoleEvent(
                    operation_id=self.SUPPORT_ROLE_KEY,
                    actor_id=None,
                    recipient_id=recipient.id,
                    recipient_username=recipient.username,
                    old_role=recipient.is_admin,
                    new_role=True,
                )
            )
            recipient.is_admin = True

    async def resolve(self, actor_id, username):
        await self.require(actor_id)
        key = normalize_username(username)
        async with self.sessions() as session:
            recipient = await session.scalar(select(User).where(User.username_key == key))
            if recipient is None:
                raise UserError("Користувача не знайдено. Отримувач має виконати /start.")
        await self.verifier.verify(recipient.id, key)
        return recipient

    async def change(self, actor_id, recipient_id, username, new_role, old_role, operation_id):
        await self.require(actor_id)
        key = normalize_username(username)
        await self.verifier.verify(recipient_id, key)
        async with self.sessions.begin() as session:
            await session.scalar(select(Guard).where(Guard.name == "admins").with_for_update())
            people = {
                u.id: u
                for u in (
                    await session.scalars(
                        select(User)
                        .where(User.id.in_(sorted({actor_id, recipient_id})))
                        .order_by(User.id)
                        .with_for_update()
                    )
                ).all()
            }
            actor, recipient = people.get(actor_id), people.get(recipient_id)
            if actor is None or not actor.is_admin:
                raise UserError("Права адміністратора вже знято.")
            event = await session.get(RoleEvent, operation_id)
            if event:
                if (event.actor_id, event.recipient_id, event.new_role) != (
                    actor_id,
                    recipient_id,
                    new_role,
                ):
                    raise UserError("Ця кнопка застаріла.")
                return
            if recipient is None or recipient.username_key != key:
                raise UserError("Нік отримувача змінився. Повтори вибір.")
            if recipient.is_admin != old_role:
                raise UserError("Права отримувача змінилися. Повтори вибір.")
            if not new_role and recipient.is_admin:
                count = await session.scalar(
                    select(func.count()).select_from(User).where(User.is_admin.is_(True))
                )
                if count <= 1:
                    raise UserError("Не можна зняти права останнього адміністратора.")
            # Preserve a revoked generation even if a subsequent role change restores access.
            from pathlib import Path

            registry = SourceRegistry.read(
                Path(__file__).resolve().parents[1] / "config/sources.json"
            )
            await reconcile_user(session, recipient, registry)
            session.add(
                RoleEvent(
                    operation_id=operation_id,
                    actor_id=actor_id,
                    recipient_id=recipient_id,
                    actor_username=actor.username,
                    recipient_username=recipient.username,
                    old_role=recipient.is_admin,
                    new_role=new_role,
                )
            )
            recipient.is_admin = new_role
            await reconcile_user(session, recipient, registry)

    async def bootstrap(self, recipient_id):
        # Only the local CLI calls this method, never a Telegram handler.
        await self.verifier.verify(recipient_id, "kv1nsom")
        async with self.sessions.begin() as session:
            guard = await session.scalar(
                select(Guard).where(Guard.name == "admins").with_for_update()
            )
            if guard is None or guard.bootstrapped:
                raise UserError("Початкове призначення вже виконано або міграції не застосовані.")
            recipient = await locked_user(session, recipient_id)
            if recipient.username_key != "kv1nsom":
                raise UserError("ID не належить зареєстрованому @kV1nsom. Потрібен його /start.")
            if await session.scalar(
                select(func.count()).select_from(User).where(User.is_admin.is_(True))
            ):
                raise UserError("Адміністратор уже існує.")
            recipient.is_admin = True
            guard.bootstrapped = True
            session.add(
                RoleEvent(
                    operation_id="bootstrap",
                    actor_id=None,
                    recipient_id=recipient_id,
                    actor_username=None,
                    recipient_username=recipient.username,
                    old_role=False,
                    new_role=True,
                )
            )
