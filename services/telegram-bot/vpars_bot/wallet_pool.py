"""Bounded, isolated persistent browsers reused between quote and confirmation.

Pages are freshly navigated on every checkout; quotes/payment flags are never reused.
No browser profile is shared across users. Revoked or changed accounts are evicted.
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass
from time import monotonic

from sqlalchemy import select

from . import autobuy_availability as autobuy
from .db import WalletAccount, WalletSession
from .wallet import SESSION_BLOCKS, WalletError, session_binding
from .wallet_accounts import wallet_account, wallet_session
from .wallet_browser import BrowserCheckout, browser_adapter, load_contract
from .wallet_mode import button_checkout

logger = logging.getLogger("vpars_bot")


def binding(account, connection=None):
    return (account.region, account.currency, account.contract_hash,
            connection.attempt_id if connection else None,
            connection.account_key if connection else None)


@dataclass
class Entry:
    binding: tuple
    busy: bool = True
    used_at: float = 0
    manager: object = None
    adapter: object = None
    prepared_item: str | None = None


class WarmBrowserPool:
    def __init__(
        self,
        sessions,
        *,
        factory=browser_adapter,
        loader=load_contract,
        capacity=4,
        idle_seconds=180,
        clock=monotonic,
    ):
        self.sessions, self.factory, self.loader = sessions, factory, loader
        self.capacity, self.idle_seconds, self.clock = max(1, capacity), idle_seconds, clock
        self.entries = {}
        self.changed = asyncio.Condition()
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        async with self.changed:
            self.closed = True
            entries = list(self.entries.values())
            self.entries.clear()
            self.changed.notify_all()
        for entry in entries:
            await self.close_entry(entry)

    async def close_entry(self, entry, exc_info=(None, None, None)):
        if entry.manager:
            manager, entry.manager = entry.manager, None
            try:
                await manager.__aexit__(*exc_info)
            except Exception as exc:
                logger.warning("Wallet browser cleanup failed (%s)", type(exc).__name__)

    async def take(self, account, connection=None):
        autobuy.require_available()
        victim = None
        async with self.changed:
            while True:
                if self.closed:
                    raise WalletError("NotConnected")
                current = self.entries.get((account.user_id, account.region))
                if current and current.busy:
                    await self.changed.wait()
                    continue
                if current:
                    if (
                        current.binding == binding(account, connection)
                        and self.clock() - current.used_at < self.idle_seconds
                        and not current.adapter.page.is_closed()
                    ):
                        current.busy = True
                        return current
                    victim = self.entries.pop((account.user_id, account.region))
                elif len(self.entries) >= self.capacity:
                    idle = [(key, entry) for key, entry in self.entries.items() if not entry.busy]
                    if not idle:
                        await self.changed.wait()
                        continue
                    key, victim = min(idle, key=lambda pair: pair[1].used_at)
                    del self.entries[key]
                entry = Entry(binding(account, connection))
                self.entries[(account.user_id, account.region)] = entry
                break
        try:
            if victim:
                await self.close_entry(victim)
            manager = (self.factory(account, background=button_checkout(self.sessions))
                       if self.factory is browser_adapter else self.factory(account))
            entry.adapter = await manager.__aenter__()
            entry.manager = manager
            return entry
        except BaseException:
            await self.release((account.user_id, account.region), entry, discard=True)
            raise

    async def release(self, user_id, entry, *, discard=False, exc_info=(None, None, None)):
        # Keep the slot busy while closing so setup cannot race a replacement profile.
        if discard:
            await self.close_entry(entry, exc_info)
        async with self.changed:
            if self.entries.get(user_id) is entry:
                if discard:
                    del self.entries[user_id]
                else:
                    entry.busy, entry.used_at = False, self.clock()
            self.changed.notify_all()

    @asynccontextmanager
    async def adapter(self, account):
        connection = None
        if self.sessions:
            async with self.sessions() as session:
                account = await wallet_account(session, account.user_id, account.region)
                if not account or not account.enabled:
                    raise WalletError("NotConnected")
                connection = await wallet_session(session, account.user_id, account.region)
                if button_checkout(self.sessions):
                    session_binding(account, connection)
                elif connection and (connection.state not in {"connected", "checkout_ready"}
                                     or connection.error == "CheckoutPaused"):
                    raise WalletError("NotConnected")
        # Re-read the local contract on EVERY lease, including a warm browser.
        contract, digest = self.loader(account.user_id, account.region)
        if (
            not account.enabled
            or not account.contract_hash
            or digest != account.contract_hash
            or contract["region"] != account.region
            or connection and contract.get("expected_account") != connection.account_key
        ):
            raise WalletError("ContractMismatch")
        entry = await self.take(account, connection)
        discard = False
        exc_info = (None, None, None)
        adapter = BrowserCheckout(
            entry.adapter.page, account, contract, prepared_item=entry.prepared_item
        )
        try:
            # New adapter state uses current budget/account and cannot retain submitted=True
            # or a previous quote. checkout() still navigates and verifies current fields.
            yield adapter
            entry.prepared_item = (
                adapter.raw["item_id"] if adapter.raw and not adapter.submitted else None
            )
        except BaseException as exc:
            discard = True
            exc_info = (type(exc), exc, exc.__traceback__)
            raise
        finally:
            await self.release(
                (account.user_id, account.region), entry, discard=discard, exc_info=exc_info,
            )

    async def maintain(self, *, prime=False):
        if not autobuy.AVAILABLE:
            return
        async with self.sessions() as session:
            connections = {(row.user_id, row.region): row for row in
                           await session.scalars(select(WalletSession))}
            accounts = list(
                await session.scalars(
                    select(WalletAccount)
                    .where(
                        WalletAccount.enabled.is_(True), WalletAccount.contract_hash.is_not(None)
                    )
                    .order_by(WalletAccount.user_id)
                )
            )
        enabled = {}
        for account in accounts:
            key = (account.user_id, account.region)
            connection = connections.get(key)
            if (connection and (connection.state not in {"connected", "checkout_ready"}
                                or connection.error == "CheckoutPaused")
                    or button_checkout(self.sessions) and (
                        connection is None or connection.error in SESSION_BLOCKS)):
                continue
            enabled[key] = account
        victims = []
        async with self.changed:
            for key, entry in list(self.entries.items()):
                account = enabled.get(key)
                if not entry.busy and (
                    account is None
                    or binding(account, connections.get(key)) != entry.binding
                    or self.clock() - entry.used_at >= self.idle_seconds
                ):
                    entry.busy = True
                    victims.append((key, entry))
        for key, entry in victims:
            await self.release(key, entry, discard=True)
        if prime:
            # Startup only. Subsequent cold accounts warm on Buy; never churn all profiles
            # when there are more enabled users than the bounded pool can retain.
            for account in list(enabled.values())[: self.capacity]:
                if account.active_order:
                    continue
                try:
                    async with self.adapter(account):
                        pass  # Launch a blank authenticated profile; no checkout or Pay.
                except Exception as exc:
                    logger.warning("Wallet browser warmup unavailable (%s)", type(exc).__name__)
            logger.info("Wallet session cache ready retained=%s capacity=%s idle_seconds=%s",
                        len(self.entries), self.capacity, self.idle_seconds)

    async def run(self):
        if not autobuy.AVAILABLE:
            return
        prime = True
        while True:
            try:
                await self.maintain(prime=prime)
                prime = False
            except Exception as exc:
                logger.warning("Wallet browser maintenance unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(2)
