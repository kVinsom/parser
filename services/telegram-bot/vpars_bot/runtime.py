import asyncio
import hmac
import json
import logging
import random
from contextlib import asynccontextmanager
from datetime import UTC, timedelta
from time import perf_counter
from uuid import uuid4

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramRetryAfter
from aiogram.fsm.storage.base import DefaultKeyBuilder
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.methods import GetUpdates
from aiogram.types import Update
from aiohttp import web
from pydantic import ValidationError
from redis.asyncio import Redis
from sqlalchemy import and_, exists, or_, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from .application import AdminService, Catalog
from .config import Settings
from .db import Guard, Inbox, User, database, utcnow
from .domain import SourceRegistry, UserError
from .latency import LatencyMetrics
from .menu_preview import make_preview_dispatcher
from .notifications import NotificationSender
from .reply_menu import make_menu_dispatcher
from .search_control import DevelopmentSearchControl, PersistentSearchControl
from .session_secrets import incoming_session_key, protected_payload, session_cipher
from .telegram import BotUI, TelegramProfileVerifier, make_dispatcher
from .transport import (
    CallbackAcknowledger,
    RedisTelegramBudget,
    TelegramDeferred,
    TelegramRequestPolicy,
)
from .update_context import (
    UpdateContext,
    chat_key,
    current_update,
    is_search_start,
    is_stop,
    private_actor,
)
from .wallet_handoff_web import handoff_server

# Autobuy parked: keep the implementations, but do not start browsers or jobs.
# from .wallet_pool import WarmBrowserPool
# from .wallet_sessions import SessionWorker
# from .wallet_worker import WalletWorker

logger = logging.getLogger("vpars_bot")


class UpdateInbox:
    def __init__(
        self,
        sessions,
        bot_id,
        *,
        concurrency=8,
        handler_timeout=20,
        idle_poll=0.5,
        metrics=None,
        control=None,
        menu_mode="full",
    ):
        self.sessions, self.bot_id = sessions, bot_id
        self.concurrency, self.handler_timeout, self.idle_poll = (
            concurrency,
            handler_timeout,
            idle_poll,
        )
        self.metrics = metrics or LatencyMetrics()
        self.control = control
        self.menu_mode = menu_mode
        self.wakeup = asyncio.Event()

    async def receive(self, update: Update):
        await self.receive_many([update])

    async def receive_many(self, updates):
        # Split at Stop so a later Start in the same polling batch captures the NEW epoch.
        batch = []
        for update in updates:
            if self.control and is_stop(update, menu_mode=self.menu_mode):
                await self._persist(batch)
                batch = []
                await self._persist([update], urgent_stop=update)
            else:
                batch.append(update)
        await self._persist(batch)

    async def _persist(self, updates, urgent_stop=None):
        if not updates:
            return
        started = perf_counter()
        async with self.sessions.begin() as session:
            if urgent_stop:
                try:
                    await self.control.stop_in_transaction(
                        session, private_actor(urgent_stop), f"inbox-stop:{urgent_stop.update_id}"
                    )
                except UserError:
                    pass  # Unregistered user: the handler explains /start, no user is created.
            starters = {
                private_actor(u) for u in updates if is_search_start(u, menu_mode=self.menu_mode)
            }
            epochs = (
                dict(
                    (
                        await session.execute(
                            select(User.id, User.delivery_epoch).where(User.id.in_(starters))
                        )
                    ).all()
                )
                if starters
                else {}
            )
            now = utcnow()
            await session.execute(
                insert(Inbox)
                .values(
                    [
                        dict(
                            bot_id=self.bot_id,
                            update_id=update.update_id,
                            payload=protected_payload(update, self.bot_id),
                            state="received",
                            attempts=0,
                            next_attempt_at=now,
                            received_at=now,
                            lane_key=chat_key(update),
                            search_epoch=epochs.get(private_actor(update))
                            if is_search_start(update, menu_mode=self.menu_mode)
                            else None,
                        )
                        for update in updates
                    ]
                )
                .on_conflict_do_nothing(index_elements=[Inbox.bot_id, Inbox.update_id])
            )
        if urgent_stop:
            self.metrics.count("stop_accepted_immediately")
        self.metrics.observe("inbox.ingest", perf_counter() - started)
        self.wakeup.set()

    async def offset(self):
        async with self.sessions() as session:
            row = await session.scalar(
                select(Inbox)
                .where(Inbox.bot_id == self.bot_id)
                .order_by(Inbox.received_at.desc(), Inbox.update_id.desc())
                .limit(1)
            )
            if row and row.received_at.replace(tzinfo=UTC) > utcnow() - timedelta(days=6):
                return row.update_id + 1
            return None

    async def claim(self):
        older = aliased(Inbox)
        now = utcnow()
        preceding = exists(
            select(1).where(
                older.bot_id == Inbox.bot_id,
                older.lane_key == Inbox.lane_key,
                older.state.in_(["received", "processing"]),
                or_(
                    older.received_at < Inbox.received_at,
                    and_(older.received_at == Inbox.received_at, older.update_id < Inbox.update_id),
                ),
            )
        )
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(Inbox)
                .where(
                    Inbox.bot_id == self.bot_id,
                    Inbox.state.in_(["received", "processing"]),
                    Inbox.next_attempt_at <= now,
                    or_(Inbox.lease_until.is_(None), Inbox.lease_until <= now),
                    ~preceding,
                )
                .order_by(Inbox.received_at, Inbox.update_id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return None
            row.state = "processing"
            row.attempts += 1
            row.lease_token = str(uuid4())
            row.lease_until = now + timedelta(seconds=self.handler_timeout + 10)
            return row

    async def once(self, dispatcher, bot):
        row = await self.claim()
        if row is None:
            return False
        await self.process(row, dispatcher, bot)
        return True

    async def process(self, claimed, dispatcher, bot):
        update_id, attempts = claimed.update_id, claimed.attempts
        payload = dict(claimed.payload)
        secret = payload.pop("_wallet_secret", None)
        update = Update.model_validate(payload, context={"bot": bot})
        secret_context = incoming_session_key.set(None)
        context_token = current_update.set(
            UpdateContext(
                update_id,
                claimed.search_epoch,
                f"inbox-stop:{update_id}" if is_stop(update, menu_mode=self.menu_mode) else None,
            )
        )
        started = perf_counter()
        self.metrics.observe(
            "inbox.queue", (utcnow() - claimed.received_at.replace(tzinfo=UTC)).total_seconds()
        )
        try:
            from . import autobuy_availability as autobuy

            if autobuy.AVAILABLE and secret and update.message:
                incoming_session_key.set(session_cipher().open(
                    secret, owner=update.message.from_user.id,
                    purpose=f"telegram:{self.bot_id}:{update_id}",
                ))
            # feed_update propagates exceptions; feed_webhook_update may acknowledge in background.
            async with asyncio.timeout(self.handler_timeout):
                await dispatcher.feed_update(bot, update)
        except Exception as exc:
            deferred = isinstance(exc, (TelegramRetryAfter, TelegramDeferred))
            delay = (
                max(0.01, float(exc.retry_after)) + 0.05
                if deferred
                else min(30, 2 ** min(attempts, 5)) + random.uniform(0, 0.25)
            )
            async with self.sessions.begin() as session:
                row = await session.scalar(
                    select(Inbox)
                    .where(
                        Inbox.bot_id == self.bot_id,
                        Inbox.update_id == update_id,
                        Inbox.lease_token == claimed.lease_token,
                    )
                    .with_for_update()
                )
                if row is not None:
                    row.state = "received" if deferred or attempts < 8 else "failed"
                    row.attempts = attempts - 1 if deferred else attempts
                    row.next_attempt_at = utcnow() + timedelta(seconds=delay)
                    row.last_error = type(exc).__name__
                    row.lease_token = row.lease_until = None
            self.metrics.count("inbox.deferred" if deferred else "inbox.errors")
            if not deferred:
                logger.warning(
                    "Update %s failed (%s); attempt %s", update_id, type(exc).__name__, attempts
                )
            handle = asyncio.get_running_loop().call_later(delay, self.wakeup.set)
            # Timer does not retain payloads and is bounded by active retry attempts.
            if delay > 3600:
                handle.cancel()  # Idle reconciliation still handles very long Telegram cooldowns.
        else:
            async with self.sessions.begin() as session:
                row = await session.scalar(
                    select(Inbox)
                    .where(
                        Inbox.bot_id == self.bot_id,
                        Inbox.update_id == update_id,
                        Inbox.lease_token == claimed.lease_token,
                    )
                    .with_for_update()
                )
                if row is not None:
                    row.state = "processed"
                    row.payload = None
                    row.last_error = None
                    row.lease_token = row.lease_until = None
            self.metrics.count("inbox.processed")
        finally:
            incoming_session_key.reset(secret_context)
            current_update.reset(context_token)
            self.metrics.observe("inbox.handler", perf_counter() - started)

    async def pump(self, dispatcher, bot):
        active = set()

        def finished(task):
            active.discard(task)
            self.wakeup.set()

        async with asyncio.TaskGroup() as group:
            while True:
                self.wakeup.clear()
                while len(active) < self.concurrency:
                    row = await self.claim()
                    if row is None:
                        break
                    task = group.create_task(self.process(row, dispatcher, bot))
                    active.add(task)
                    task.add_done_callback(finished)
                try:
                    await asyncio.wait_for(self.wakeup.wait(), timeout=self.idle_poll)
                except TimeoutError:
                    pass


async def polling(bot, inbox, acknowledger=None):
    await bot.delete_webhook(drop_pending_updates=False)
    offset = await inbox.offset()
    while True:
        try:
            updates = await bot(
                GetUpdates(
                    offset=offset, timeout=25, allowed_updates=["message", "callback_query"]
                ),
                request_timeout=35,
            )
            await inbox.receive_many(updates)
            for update in updates:
                if acknowledger:
                    acknowledger.offer(update)
            if updates:
                offset = max(update.update_id for update in updates) + 1
        except asyncio.CancelledError:
            raise
        except TelegramRetryAfter as exc:
            await asyncio.sleep(exc.retry_after + 0.05)
        except Exception as exc:
            logger.warning("Receiving updates failed (%s)", type(exc).__name__)
            await asyncio.sleep(2)


def webhook_app(settings, inbox, healthcheck=None):
    app = web.Application(client_max_size=1_048_576)

    async def receive(request):
        provided = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if not hmac.compare_digest(provided, settings.webhook_secret.get_secret_value()):
            raise web.HTTPForbidden()
        try:
            payload = await request.json()
            update = Update.model_validate(payload)
        except (ValueError, ValidationError):
            raise web.HTTPBadRequest() from None
        try:
            await inbox.receive(update)
        except Exception:
            raise web.HTTPServiceUnavailable() from None
        if update.callback_query:
            # No extra HTTP round trip; acknowledgement is not confirmation of a business action.
            return web.json_response(
                {"method": "answerCallbackQuery", "callback_query_id": update.callback_query.id}
            )
        return web.Response(text="OK")

    async def health(request):
        if healthcheck:
            try:
                await healthcheck()
            except Exception:
                raise web.HTTPServiceUnavailable() from None
        return web.json_response(
            {"status": "ok", "mode": "webhook", "latency": inbox.metrics.snapshot()}
        )

    app.router.add_post(settings.webhook_path, receive)
    app.router.add_get("/health", health)
    return app


async def webhook(settings, bot, inbox, healthcheck):
    runner = web.AppRunner(webhook_app(settings, inbox, healthcheck), access_log=None)
    await runner.setup()
    try:
        await web.TCPSite(runner, settings.http_host, settings.http_port).start()
        await bot.set_webhook(
            settings.webhook_base_url.rstrip("/") + settings.webhook_path,
            secret_token=settings.webhook_secret.get_secret_value(),
            allowed_updates=["message", "callback_query"],
            drop_pending_updates=False,
            max_connections=settings.webhook_max_connections,
        )
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


@asynccontextmanager
async def exclusive_receiver(engine, bot_id):
    # The PostgreSQL session lock prevents polling/webhook overlap across processes/machines.
    async with engine.connect() as connection:
        params = {"bot_id": bot_id % 2147483647}
        locked = await connection.scalar(
            text("SELECT pg_try_advisory_lock(198401, :bot_id)"), params
        )
        if not locked:
            raise RuntimeError("Another receiver for this bot is already running")
        try:
            yield connection
        finally:
            await connection.execute(text("SELECT pg_advisory_unlock(198401, :bot_id)"), params)


async def monitor_receiver_lock(connection):
    while True:
        await asyncio.sleep(3)
        # Failure cancels the receiver and inbox processor through TaskGroup.
        await connection.execute(text("SELECT 1"))


async def report_latency(metrics):
    while True:
        await asyncio.sleep(60)
        logger.info("Latency statistics: %s", json.dumps(metrics.snapshot()))


async def run(settings: Settings):
    if not settings.telegram_bot_token.get_secret_value():
        raise UserError(
            "Додай TELEGRAM_BOT_TOKEN у локальний .env перед запуском бота. Тести працюють без токена."
        )
    engine, sessions = database(settings.database_url.get_secret_value())
    redis = Redis.from_url(settings.redis_url.get_secret_value())
    metrics = LatencyMetrics()
    session = AiohttpSession(
        timeout=settings.telegram_request_timeout, limit=settings.telegram_connection_limit
    )
    bot = Bot(
        settings.telegram_bot_token.get_secret_value(),
        session=session,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    session.middleware(
        TelegramRequestPolicy(
            RedisTelegramBudget(
                redis,
                bot.id,
                per_second=settings.telegram_messages_per_second,
                chat_interval=settings.telegram_chat_interval,
            ),
            metrics,
            concurrency=settings.inbox_concurrency,
        )
    )
    acknowledger = CallbackAcknowledger(bot, metrics)
    storage = RedisStorage(
        redis=redis,
        key_builder=DefaultKeyBuilder(with_bot_id=True),
        state_ttl=86400,
        data_ttl=86400,
    )
    isolation = storage.create_isolation(lock_kwargs={"timeout": settings.handler_timeout + 10})
    try:
        await redis.ping()
        async with sessions() as session:
            if await session.get(Guard, "admins") is None:
                raise RuntimeError("Apply Alembic migrations before starting")
        registry = SourceRegistry.read(settings.source_config)
        await registry.exchange.refresh(redis)
        catalog = Catalog(sessions, registry)
        control = (
            DevelopmentSearchControl(sessions, registry, app_env=settings.app_env)
            if settings.search_adapter == "demo"
            else PersistentSearchControl(sessions, registry)
        )
        admins = AdminService(sessions, TelegramProfileVerifier(bot))
        ui = BotUI(
            catalog, control, admins, registry, development=settings.app_env == "development"
        )
        if settings.menu_mode == "full":
            dispatcher = make_dispatcher(storage, isolation, ui, acknowledge_callbacks=False)
        elif settings.menu_mode == "menu":
            dispatcher = make_menu_dispatcher(
                storage, isolation, catalog, control, admins=admins, acknowledge_callbacks=False
            )
        else:
            dispatcher = make_preview_dispatcher(
                storage, isolation, catalog, acknowledge_callbacks=False
            )
        inbox = UpdateInbox(
            sessions,
            bot.id,
            concurrency=settings.inbox_concurrency,
            handler_timeout=settings.handler_timeout,
            metrics=metrics,
            control=control if settings.menu_mode != "preview" else None,
            menu_mode=settings.menu_mode,
        )

        async def healthcheck():
            await redis.ping()
            async with sessions() as session:
                await session.execute(text("SELECT 1"))

        # WarmBrowserPool and payment/session workers stay disabled for this release.
        # async with WarmBrowserPool(sessions, capacity=settings.wallet_cache_capacity,
        #                            idle_seconds=settings.wallet_cache_idle_seconds) as wallets:
        async with exclusive_receiver(engine, bot.id) as connection, handoff_server(sessions, settings):
            async with asyncio.TaskGroup() as group:
                group.create_task(registry.exchange.run(redis))
                group.create_task(monitor_receiver_lock(connection))
                group.create_task(inbox.pump(dispatcher, bot))
                group.create_task(NotificationSender(sessions, bot).run())
                # group.create_task(wallets.run())
                # group.create_task(WalletWorker(sessions, wallets.adapter).run())
                # group.create_task(SessionWorker(sessions).run())
                group.create_task(report_latency(metrics))
                group.create_task(
                    polling(bot, inbox, acknowledger)
                    if settings.update_mode == "polling"
                    else webhook(settings, bot, inbox, healthcheck)
                )
    finally:
        await acknowledger.close()
        await isolation.close()
        await storage.close()
        await bot.session.close()
        await engine.dispose()
