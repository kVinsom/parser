from dataclasses import asdict, dataclass
from datetime import UTC, timedelta
from typing import Protocol

from sqlalchemy import select

from .application import command, locked_user, owned_search, previous, remember, selected_sources
from .db import Search, SearchRun, User, utcnow
from .domain import CapabilityProvider, Filters, SourceRegistry, UserError, conflict_notice
from .entitlements import MESSAGES, access_error, allowed_sources, plan_for, reconcile_user
from .formatting import filter_name
from .update_context import current_update


@dataclass(frozen=True)
class WorkToken:
    user_id: int
    search_id: str
    source_id: str
    delivery_epoch: int
    generation: int
    filter_version: int


class SearchControlPort(Protocol):
    async def start(self, user_id: int, operation_id: str, search_id: str | None = None) -> int: ...
    async def stop(self, user_id: int, operation_id: str) -> int: ...
    async def status(self, user_id: int) -> dict[str, str]: ...
    async def problems(self, user_id: int) -> list[str]: ...


def run_state(run: SearchRun) -> str:
    if run.state == "active" and (
        run.active_until is None or run.active_until.replace(tzinfo=UTC) <= utcnow()
    ):
        return "requested"
    return run.state


class PersistentSearchControl:
    """Persist intent before returning; does not start a monitoring process."""

    def __init__(
        self, sessions, registry: SourceRegistry, capabilities: CapabilityProvider | None = None
    ):
        self.sessions = sessions
        self.registry = registry
        self.capabilities = capabilities or registry

    def requested_state(self):
        return "requested"

    async def start(self, user_id, operation_id, search_id=None):
        # Capability calls may be remote in a future adapter: never hold DB locks across them.
        async with self.sessions() as session:
            snapshot_user = await session.get(User, user_id)
            if snapshot_user is None:
                raise UserError("Спочатку виконай /start.")
            expected_epoch = snapshot_user.delivery_epoch
            plan = await plan_for(session, snapshot_user)
            if not plan.filters:
                raise UserError(MESSAGES["SubscriptionRequired"])
            enabled = allowed_sources(snapshot_user, self.registry, plan)
            if not enabled:
                raise UserError("Увімкни хоча б один майданчик у меню «Маркетплейси».")
            received_epoch = current_update.get().search_epoch
            if received_epoch is not None and received_epoch != expected_epoch:
                raise UserError(
                    "Цей запит на запуск надійшов до зупинки. Натисни «Почати пошук» ще раз."
                )
            query = select(Search).where(Search.user_id == user_id, Search.archived.is_(False))
            if search_id:
                query = query.where(Search.id == search_id)
            items = (await session.scalars(query)).all()
            if not items:
                raise UserError("Спочатку збережи хоча б один набір через «Додати фільтр».")
            versions = {item.id: (item.version, item.enabled) for item in items}
            if not any(item.enabled for item in items):
                raise UserError("Увімкни хоча б один фільтр у меню «✅ Включені фільтри».")
            eligible = {}
            for item in items:
                if not item.enabled:
                    continue
                if await access_error(session, snapshot_user, item, self.registry):
                    continue
                saved = Filters.model_validate(item.filters)
                sources = enabled if saved.follow_enabled_sources else set(saved.sources) & enabled
                if not sources:
                    continue
                eligible[item.id] = {}
                for source_id in sorted(sources):
                    filters = self.registry.for_source(saved, source_id)
                    eligible[item.id][source_id] = filters.model_dump(mode="json")
                    issues = await self.capabilities.issues(filters)
                    if issues:
                        raise UserError(
                            "Запуск недоступний. " + " ".join(issues) + " Зміни фільтри."
                        )
            if not eligible:
                raise UserError(
                    "Немає фільтрів, дозволених поточним тарифом і вибором регіонів. Перевір налаштування та підписку."
                )
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            replay = await previous(session, user_id, operation_id, "start")
            if replay:
                return replay["count"]
            if user.delivery_epoch != expected_epoch:
                raise UserError(
                    "Пошук уже зупинено. Для нового запуску натисни «Почати пошук» ще раз."
                )
            if allowed_sources(user, self.registry, await plan_for(session, user)) != enabled:
                raise UserError("Вибір маркетплейсів змінився. Натисни «Почати пошук» ще раз.")
            current = (await session.scalars(query)).all()
            if {item.id: (item.version, item.enabled) for item in current} != versions:
                raise UserError("Список змінився. Натисни «Почати пошук» ще раз.")
            # Recheck the plan after remote capability calls, inside the user lock.
            for item in current:
                if item.id in eligible:
                    for source_id in eligible[item.id]:
                        error = await access_error(session, user, item, self.registry, source_id)
                        if error:
                            raise UserError(MESSAGES[error])
            await reconcile_user(session, user, self.registry)
            for item in current:
                if item.id not in eligible:
                    continue
                for source_id, source_filters in eligible[item.id].items():
                    run = await session.get(SearchRun, (item.id, source_id))
                    if run is None:
                        run = SearchRun(
                            search_id=item.id, source_id=source_id, generation=0, state="stopped"
                        )
                        session.add(run)
                    if run.state not in {"stopped", "paused_limit"}:
                        continue
                    run.generation += 1
                    run.state = self.requested_state()
                    run.active_until = None
                    run.last_error = None
                    token = WorkToken(
                        user_id,
                        item.id,
                        source_id,
                        user.delivery_epoch,
                        run.generation,
                        item.version,
                    )
                    command(
                        session,
                        user_id,
                        "start",
                        {
                            "token": asdict(token),
                            "filters": source_filters,
                            "simulation": run.state == "demo",
                        },
                    )
            remember(session, user_id, operation_id, "start", {"count": len(eligible)})
            return len(eligible)

    async def stop(self, user_id, operation_id):
        operation_id = current_update.get().stop_key or operation_id
        async with self.sessions.begin() as session:
            return await self.stop_in_transaction(session, user_id, operation_id)

    async def stop_in_transaction(self, session, user_id, operation_id):
        """Admission hook: Stop and its durable incoming update commit atomically."""
        user = await locked_user(session, user_id)
        replay = await previous(session, user_id, operation_id, "stop")
        if replay:
            return replay["epoch"]
        user.delivery_epoch += 1
        runs = (
            await session.scalars(select(SearchRun).join(Search).where(Search.user_id == user_id))
        ).all()
        for run in runs:
            run.generation += 1
            run.state = "stopped"
            run.active_until = None
            run.last_error = None
        command(session, user_id, "stop", {"delivery_epoch": user.delivery_epoch})
        remember(session, user_id, operation_id, "stop", {"epoch": user.delivery_epoch})
        return user.delivery_epoch

    async def status(self, user_id):
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            await reconcile_user(session, user, self.registry)
            runs = (
                await session.scalars(
                    select(SearchRun)
                    .join(Search)
                    .where(Search.user_id == user_id, Search.archived.is_(False))
                )
            ).all()
            states: dict[str, str] = {}
            priority = {"stopped": 0, "paused_limit": 0, "active": 1, "demo": 2, "requested": 3}
            for run in runs:
                state = run_state(run)
                if priority[state] >= priority.get(states.get(run.search_id, "stopped"), 0):
                    states[run.search_id] = state
            return states

    async def problems(self, user_id):
        async with self.sessions() as session:
            messages = []
            for item in await session.scalars(
                select(Search)
                .where(
                    Search.user_id == user_id, Search.archived.is_(False), Search.enabled.is_(True)
                )
                .order_by(Search.id)
            ):
                if notice := conflict_notice(item.filters):
                    messages.append(f"{filter_name(item.filters)[:100]}: {notice}")
            rows = await session.execute(
                select(Search, SearchRun)
                .join(SearchRun)
                .where(
                    Search.user_id == user_id,
                    Search.archived.is_(False),
                    SearchRun.state.in_(["requested", "active", "paused_limit"]),
                    SearchRun.last_error.is_not(None),
                )
                .order_by(Search.id, SearchRun.source_id)
                .limit(6)
            )
            for item, run in rows:
                source = self.registry.sources.get(run.source_id)
                if source is None:
                    continue
                name = filter_name(item.filters)
                detail = "Сайт тимчасово недоступний. Повторюємо перевірку."
                if run.last_error in MESSAGES:
                    detail = MESSAGES[run.last_error]
                if run.last_error == "GatewayTimeout":
                    detail = "Сторінка завантажувалася надто довго. Повторюємо перевірку."
                elif run.last_error in {"GatewayServiceError", "GatewayUnavailable"}:
                    detail = "Помилка сервісу завантаження. Повторюємо перевірку."
                elif run.last_error == "UnexpectedContent":
                    detail = "Не вдалося підтвердити вміст сторінки. Перегляд ще не завершений."
                elif run.last_error == "AccessBlocked":
                    detail = "Vinted обмежив доступ. Чекаємо перед наступною перевіркою."
                elif run.last_error == "SourceRateLimited":
                    detail = "Vinted обмежив частоту запитів. Чекаємо перед наступною перевіркою."
                elif run.last_error == "ExchangeRateUnavailable":
                    detail = "Курс валют тимчасово недоступний. Пошук відновиться після оновлення курсу."
                if run.last_error.startswith("UnsupportedFilter"):
                    detail = "Не вдалося перевірити один із фільтрів."
                    if "sizes" in run.last_error:
                        detail = "Не вдалося перевірити розміри в цьому каталозі."
                messages.append(f"{name[:100]} ({source.label}): {detail}")
            # Keep the status message within Telegram's text limit even when
            # many saved searches contain conflicting phrases.
            return messages[:6]

    async def acknowledge(self, token: WorkToken, *, lease_seconds: int = 60) -> bool:
        """Integration hook for a trusted worker after real monitoring starts, with a lease."""
        if not 1 <= lease_seconds <= 300:
            raise ValueError("Worker lease must be 1–300 seconds")
        async with self.sessions.begin() as session:
            user = await locked_user(session, token.user_id)
            item = await owned_search(session, token.user_id, token.search_id)
            run = await session.get(SearchRun, (token.search_id, token.source_id))
            if (
                not self._current(user, item, run, token)
                or run.state == "demo"
                or await access_error(session, user, item, self.registry, token.source_id)
            ):
                return False
            run.state = "active"
            run.last_error = None
            run.active_until = utcnow() + timedelta(seconds=lease_seconds)
            return True

    async def can_deliver(self, token: WorkToken) -> bool:
        """Future notifier must call immediately before sending for at least one matching token."""
        async with self.sessions() as session:
            user = await session.get(User, token.user_id)
            item = await session.get(Search, token.search_id)
            run = await session.get(SearchRun, (token.search_id, token.source_id))
            return (
                self._current(user, item, run, token)
                and run_state(run) == "active"
                and not await access_error(session, user, item, self.registry, token.source_id)
            )

    def _current(self, user, item, run, token):
        return bool(
            user
            and item
            and run
            and item.user_id == user.id
            and not item.archived
            and item.enabled
            and user.delivery_epoch == token.delivery_epoch
            and item.version == token.filter_version
            and run.generation == token.generation
            and run.state in {"requested", "active"}
            and (
                item.filters.get("follow_enabled_sources")
                or token.source_id in item.filters["sources"]
            )
            and token.source_id in self.registry.sources
            and token.source_id in selected_sources(user, self.registry)
            and not any(
                item.filters.get(field)
                for field in self.registry.sources[token.source_id].unsupported
            )
            and all(
                r in self.registry.sources[token.source_id].regions
                for r in (
                    self.registry.sources[token.source_id].regions
                    if item.filters.get("follow_enabled_sources")
                    else item.filters["sources"][token.source_id]
                )
            )
        )


class DevelopmentSearchControl(PersistentSearchControl):
    def __init__(self, sessions, registry, *, app_env):
        if app_env != "development":
            raise ValueError("Simulation requires development environment")
        super().__init__(sessions, registry)

    def requested_state(self):
        return "demo"
