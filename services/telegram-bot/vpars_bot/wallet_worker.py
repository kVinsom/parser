"""Isolated checkout queue. Network failure never retries an attempted payment."""

import asyncio
import logging
from datetime import UTC

from sqlalchemy import case, select

from . import autobuy_availability as autobuy
from .application import locked_user
from .db import WalletOrder, utcnow
from .wallet import (
    WalletError,
    WalletService,
    listing_identity,
    notice,
    session_binding,
    stored_quote,
)
from .wallet_accounts import wallet_account, wallet_session
from .wallet_mode import button_checkout
from .wallet_signals import signals, wait_for_work

logger = logging.getLogger("vpars_bot")


class WalletWorker:
    def __init__(self, sessions, adapter_factory, *, concurrency=4):
        self.sessions = sessions
        self.service = WalletService(sessions)
        self.factory = adapter_factory
        self.concurrency = max(1, concurrency)

    async def recover(self):
        if not autobuy.AVAILABLE:
            return
        # Call only with the bot receiver singleton lock held, before claiming any work.
        interrupted = ["checking", "executing", "bot_checking", "bot_executing", "paying"]
        if button_checkout(self.sessions):
            # Requests from another checkout mode cannot become an approved bot purchase.
            interrupted += ["requested", "queued", "quoted", "human_waiting", "human_active",
                            "balance_waiting", "balance_checking"]
        else:
            interrupted += ["bot_requested", "bot_quoted", "bot_queued"]
        async with self.sessions.begin() as session:
            orders = list(
                await session.scalars(
                    select(WalletOrder).where(
                        WalletOrder.state.in_(interrupted)
                    )
                )
            )
            for order in orders:
                await locked_user(session, order.user_id)
                acc = await wallet_account(session, order.user_id, listing_identity(order.url)[0])
                if order.state in {"paying", "human_active"}:
                    order.state, order.error = "unknown", "Interrupted"
                else:
                    order.state, order.error, acc.active_order = "failed", "Interrupted", None
                order.updated_at = utcnow()
                notice(session, order)

    async def claim(self):
        if not autobuy.AVAILABLE:
            return None
        # One dispatcher claims serially under exclusive_receiver. Execution may overlap
        # across accounts; account.active_order and user locks preserve one purchase each.
        async with self.sessions.begin() as session:
            pending = (["bot_requested", "bot_queued"] if button_checkout(self.sessions)
                       else ["requested", "queued"])
            expired = list(
                await session.scalars(
                    select(WalletOrder).where(
                        WalletOrder.state.in_(["quoted", "bot_quoted"]),
                        WalletOrder.expires_at <= utcnow(),
                    )
                )
            )
            for old in expired:
                await locked_user(session, old.user_id)
                await session.refresh(old)
                if old.state in {"quoted", "bot_quoted"}:
                    acc = await wallet_account(session, old.user_id, listing_identity(old.url)[0])
                    old.state, old.error, acc.active_order = "failed", "Expired", None
                    notice(session, old)
            order = await session.scalar(
                select(WalletOrder)
                .where(WalletOrder.state.in_(pending))
                .order_by(case((WalletOrder.state.in_(["queued", "bot_queued"]), 0), else_=1), WalletOrder.created_at)
                .limit(1)
            )
            if order is None:
                return None
            user = await locked_user(session, order.user_id)
            await session.refresh(order)
            if order.state not in pending:
                return None
            acc = await wallet_account(session, order.user_id, listing_identity(order.url)[0])
            error, _ = await self.service.access(session, user, acc, order=order)
            if error:
                order.state, order.error = "failed", error
                if acc:
                    acc.active_order = None
                notice(session, order)
                return None
            order.state = {"requested": "checking", "queued": "executing",
                           "bot_requested": "bot_checking", "bot_queued": "bot_executing"}[order.state]
            order.updated_at = utcnow()
            return order, acc

    async def once(self):
        claimed = await self.claim()
        if claimed is None:
            return False
        await self.execute(claimed)
        return True

    async def execute(self, claimed):
        if not autobuy.AVAILABLE:
            return False
        order, acc = claimed
        attempted = False
        try:
            if not acc or not acc.enabled or not acc.contract_hash or acc.active_order != order.id:
                raise WalletError("NotConnected")
            if order.state == "executing" and order.expires_at.replace(tzinfo=UTC) <= utcnow():
                raise WalletError("Expired")
            binding = None
            if order.state in {"bot_checking", "bot_executing"}:
                if not button_checkout(self.sessions):
                    raise WalletError("NotConnected")
                async with self.sessions() as session:
                    binding = session_binding(acc, await wallet_session(session, acc.user_id, acc.region))
                if not order.quote or order.quote.get("session_binding") != binding:
                    raise WalletError("NotConnected")
                if not order.expires_at or order.expires_at.replace(tzinfo=UTC) <= utcnow():
                    raise WalletError("Expired")
            async with asyncio.timeout(75):
                async with self.factory(acc) as adapter:
                    quote = await adapter.checkout(order)
                    quote.check(order, acc)
                    if binding and quote.account_key != binding[1]:
                        raise WalletError("NotConnected")
                    if order.state in {"checking", "bot_checking"}:
                        await self.service.finish(order.id, "bot_quoted" if binding else "quoted",
                                                  quote=quote, expected_binding=binding)
                        return True
                    if not quote.same_purchase(stored_quote(order)):
                        raise WalletError("CheckoutChanged")
                    # Commit the ambiguity barrier BEFORE the only irreversible operation.
                    await self.service.finish(order.id, "paying", quote=quote if binding else None,
                                              expected_binding=binding)
                    attempted = True
                    external_id = await adapter.pay(order, quote)
                    await self.service.finish(order.id, "succeeded", external_id=external_id)
        except Exception as exc:
            code = str(exc) if isinstance(exc, WalletError) else "ContractMismatch"
            # If success already committed but browser cleanup failed, retain success.
            async with self.sessions() as session:
                current = await session.get(WalletOrder, order.id)
            if current.state in {"succeeded", "quoted", "bot_quoted"}:
                return True
            await self.service.finish(
                order.id,
                "unknown" if attempted or current.state == "paying" else "failed",
                error=code,
            )
        signals(self.sessions).notifications.set()

    async def run(self):
        if not autobuy.AVAILABLE:
            return
        wake = signals(self.sessions).wallet
        running = set()
        recovering = True
        try:
            while True:
                wake.clear()
                try:
                    if recovering:
                        # Never recover while another account is still executing Pay.
                        if running:
                            await asyncio.gather(*running, return_exceptions=True)
                            running.clear()
                        await self.recover()
                        recovering = False
                    for task in list(running):
                        if task.done():
                            running.remove(task)
                            task.result()
                    while len(running) < self.concurrency:
                        claimed = await self.claim()
                        signals(self.sessions).notifications.set()
                        if claimed is None:
                            break
                        task = asyncio.create_task(self.execute(claimed))
                        task.add_done_callback(lambda _: wake.set())
                        running.add(task)
                    await wait_for_work(wake)
                except Exception as exc:
                    logger.warning("Wallet queue unavailable (%s)", type(exc).__name__)
                    recovering = True
                    await asyncio.sleep(5)
        finally:
            for task in running:
                task.cancel()
            await asyncio.gather(*running, return_exceptions=True)
