"""Owned checkout browser, with a verified adapter for the balance-first policy."""

import asyncio
import json
import math
from contextlib import asynccontextmanager
from urllib.parse import urlsplit

from .domain import UserError
from .wallet import REGIONS, WalletError, listing_identity
from .wallet_address import address_requirement, open_address_form
from .wallet_browser import (
    cdp_endpoint,
    close_popup,
    contract_path,
    dismiss_dialog,
    listing_buy_ready,
    profile_lock,
    wallet_context,
    wallet_page,
)
from .wallet_navigation import navigation_guard

VIEWPORT = {"width": 420, "height": 820}
BUY = 'button[data-testid="item-buy-button"]'
LOGIN = '[data-testid="header--login-button"]'
CAPTCHA_HOST = "geo.captcha-delivery.com"


def same_origin(url, origin):
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}" == origin


def pointer_coordinates(event):
    x, y = event.get("x"), event.get("y")
    if any(type(v) not in {int, float} or not math.isfinite(v) for v in (x, y)):
        raise ValueError("Invalid pointer")
    if not (0 <= x < VIEWPORT["width"] and 0 <= y < VIEWPORT["height"]):
        raise ValueError("Invalid pointer")
    return x, y


class HumanBrowser:
    def __init__(self, page, order, identity):
        self.page, self.order, self.identity = page, order, identity
        self.region, self.item_id = listing_identity(order.url)
        self.origin = "https://" + REGIONS[self.region][0]
        self.buy_clicked = False
        self.challenge_seen = False
        self.pointer_down = False
        self.lock = asyncio.Lock()

    async def state(self):
        if not same_origin(self.page.url, self.origin):
            return "unavailable"
        for frame in await self.page.locator("iframe[src]").all():
            source = urlsplit(await frame.get_attribute("src") or "")
            if (source.scheme == "https" and source.hostname == CAPTCHA_HOST
                    and source.path.rstrip("/") == "/captcha" and await frame.is_visible()):
                self.challenge_seen = True
                return "challenge"
        if await self.page.locator(LOGIN).is_visible():
            return "login"
        if await self.page.locator('[data-testid="checkout-generic-error-modal"]').is_visible():
            return "rejected"
        if await self.page.locator("#onetrust-banner-sdk").is_visible():
            return "consent"
        if urlsplit(self.page.url).path.rstrip("/") == "/checkout":
            return await address_requirement(self.page, self.origin) or "checkout"
        return "vinted"

    async def prepare(self, *, resume=False):
        """One observed listing Buy, never a guessed checkout URL or payment selector."""
        async with self.lock:
            state = await self.state()
            if state in {"challenge", "login", "unavailable", "consent", "rejected"}:
                return
            if state in {"checkout", "address_required", "address_editing"}:
                # Keep the address selected by Vinted. Do not pick a different one
                # or repeat Buy when returning from the user's address form.
                if state == "address_required" and resume:
                    await self.require_identity()
                    await open_address_form(self.page, self.origin)
                return
            if self.buy_clicked and not (resume and self.challenge_seen):
                return
            try:
                if listing_identity(self.page.url) != (self.region, self.item_id):
                    return
            except WalletError:
                return
            # Confirm the profile binding without sending any cookie or identity to the client.
            await self.require_identity()
            button = self.page.locator(BUY)
            if not await listing_buy_ready(self.page, BUY):
                return
            if await self.state() != "vinted":
                return
            await self.require_identity()
            # Set before dispatch: timeouts never repeat a click with an ambiguous result.
            self.buy_clicked = True
            self.challenge_seen = False
            await button.click(timeout=5000)

    async def wait_for_checkout(self):
        """Wait for the one Buy already sent; never click again while loading."""
        from playwright.async_api import TimeoutError as BrowserTimeout

        if not self.buy_clicked:
            return
        try:
            await self.page.wait_for_function("""() => {
              const visible = s => [...document.querySelectorAll(s)].some(e =>
                e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
              return visible('[data-testid="checkout"]')
                || visible('[data-testid="checkout-generic-error-modal"]')
                || visible('[data-testid="header--login-button"]')
                || visible('iframe[src^="https://geo.captcha-delivery.com/captcha"]');
            }""", timeout=15000)
        except BrowserTimeout:
            # Show the actual page for manual action; balance validation still fails closed.
            return

    async def require_identity(self):
        from .wallet_sessions import claims

        cookies = await self.page.context.cookies(self.origin)
        access = [c["value"] for c in cookies if c["name"] == "access_token_web"]
        try:
            valid = len(access) == 1 and claims(access[0])["sub"] == self.identity
        except WalletError:
            valid = False
        if not valid:
            raise UserError("Віднови вхід у Vinted цього регіону через меню бота.")

    async def balance_adapter(self, account):
        from .wallet_browser import BrowserCheckout, load_contract

        await self.require_identity()
        if not account.enabled or not account.contract_hash:
            raise WalletError("NotConnected")
        contract, digest = load_contract(account.user_id, account.region)
        if digest != account.contract_hash or contract["expected_account"] != self.identity:
            raise WalletError("ContractMismatch")
        # Reuse this exact checkout/tab. Do not navigate or re-select its delivery.
        return BrowserCheckout(self.page, account, contract)

    async def frame(self):
        async with self.lock:
            if not same_origin(self.page.url, self.origin):
                raise UserError("Vinted відкрив інший сайт. Цей перехід не підтримується.")
            state = await self.state()
            jpeg = await self.page.screenshot(type="jpeg", quality=75, timeout=5000)
            return state, jpeg

    async def input(self, event):
        """Only bounded human pointer/key events. No client scripts, URLs or selectors."""
        kind = event.get("type")
        async with self.lock:
            if not same_origin(self.page.url, self.origin):
                raise UserError("Сторінка Vinted недоступна.")
            if kind in {"down", "move", "up"}:
                x, y = pointer_coordinates(event)
                if kind == "down" and self.pointer_down:
                    raise ValueError("Pointer already down")
                if kind == "up" and not self.pointer_down:
                    return
                await self.page.mouse.move(x, y)
                if kind == "down":
                    self.pointer_down = True
                    await self.page.mouse.down()
                elif kind == "up":
                    self.pointer_down = False
                    await self.page.mouse.up()
            elif kind == "scroll":
                delta = event.get("delta")
                if type(delta) not in {int, float} or not math.isfinite(delta) or abs(delta) > 1500:
                    raise ValueError("Invalid scroll")
                if "x" in event or "y" in event:
                    await self.page.mouse.move(*pointer_coordinates(event))
                await self.page.mouse.wheel(0, delta)
            elif kind == "text":
                value = event.get("value")
                if not isinstance(value, str) or len(value) > 256 or any(ord(c) < 32 for c in value):
                    raise ValueError("Invalid text")
                await self.page.keyboard.insert_text(value)
            elif kind == "key":
                if event.get("value") not in {"Backspace", "Delete", "Tab", "Enter", "Escape",
                                              "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"}:
                    raise ValueError("Invalid key")
                await self.page.keyboard.press(event["value"])
            else:
                raise ValueError("Invalid input")


@asynccontextmanager
async def human_browser(order, account, identity):
    from . import autobuy_availability as autobuy

    autobuy.require_available()
    from playwright.async_api import async_playwright

    path = contract_path(account.user_id, account.region)
    config = json.loads(path.read_text("utf-8")) if path.is_file() else {}
    async with profile_lock(account.user_id, account.region), async_playwright() as driver:
        async with wallet_context(
            driver, account.user_id, account.region, config, headless=True,
            service_workers="block", viewport=VIEWPORT, locale="en-GB",
        ) as context, wallet_page(context, config, viewport=VIEWPORT) as page:
            browser = HumanBrowser(page, order, identity)
            if cdp_endpoint(config):
                # Check BEFORE navigation/streaming, including when a challenge is shown.
                await browser.require_identity()

            page.on("popup", close_popup)
            page.on("dialog", dismiss_dialog)
            async with navigation_guard(page, browser.origin):
                await page.goto(order.url, wait_until="domcontentloaded", timeout=25000)
                await browser.prepare()
                await browser.wait_for_checkout()
                yield browser
