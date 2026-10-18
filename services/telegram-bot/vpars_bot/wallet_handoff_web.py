"""Private Telegram Mini App transport for a person's checkout browser."""

import asyncio
import contextlib
import json
import logging
import re
import secrets
from dataclasses import dataclass, field
from datetime import UTC
from pathlib import Path
from time import monotonic

from aiohttp import WSMsgType, web

from . import autobuy_availability as autobuy
from .db import WalletOrder, utcnow
from .domain import UserError
from .wallet import ERRORS, WalletError, status_text
from .wallet_balance import FALLBACK, balance_purchase
from .wallet_handoff import OPEN_STATES, HandoffService, telegram_owner
from .wallet_handoff_browser import VIEWPORT, human_browser

ASSETS = Path(__file__).with_name("handoff_assets")
logger = logging.getLogger("vpars_bot")


def soon_app():
    """Retired public links cannot authenticate, launch a browser or accept input."""
    app = web.Application(client_max_size=1024)

    async def soon(request):
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                   "Content-Security-Policy": "default-src 'none'"}
        if request.path.endswith("/auth"):
            return web.json_response({"error": autobuy.SOON}, status=403, headers=headers)
        if request.path == "/health":
            return web.json_response({"status": "ok", "mode": "soon"}, headers=headers)
        return web.Response(text=autobuy.SOON, headers=headers)

    app.router.add_route("*", "/{path:.*}", soon)
    return app


@dataclass
class Window:
    owner: int
    order_id: str
    manager: object = None
    browser: object = None
    attached: bool = True
    disconnected: float = 0
    deadline: float = 0
    socket: object = None
    frames: dict = field(default_factory=dict)
    closing: bool = False
    launching: bool = True
    result: str | None = None
    note: str | None = None
    identity: str | None = None


class HandoffWindows:
    def __init__(self, service, *, factory=human_browser, capacity=4):
        self.service, self.factory, self.capacity = service, factory, capacity
        self.windows = {}
        self.lock = asyncio.Lock()

    async def attach(self, owner, order_id):
        order, account, identity = await self.service.authorize(owner, order_id)
        async with self.lock:
            window = self.windows.get(order_id)
            if window:
                if window.owner != owner or window.attached or monotonic() >= window.deadline:
                    raise UserError("Це вікно вже відкрите або сеанс завершено.")
                window.attached = True
                return window
            if len(self.windows) >= self.capacity:
                raise UserError("Усі вікна зайняті. Спробуй ще раз за хвилину.")
            if order.state in {"human_active", "balance_checking"}:
                # An earlier browser was lost. Never rebuild a possibly paid checkout.
                raise UserError("Попередній сеанс завершився. Перевір замовлення у Vinted.")
            window = Window(owner, order_id, identity=identity, deadline=monotonic() + max(
                0, (order.expires_at.replace(tzinfo=UTC) - utcnow()).total_seconds(),
            ))
            self.windows[order_id] = window
        try:
            if order.state == "balance_waiting":
                order, account, identity = await self.service.authorize(
                    owner, order_id, claim_balance=True,
                )
            manager = self.factory(order, account, identity)
            window.manager = manager
            window.browser = await manager.__aenter__()
            if order.state == "balance_checking":
                result = await balance_purchase(self.service, order, account, identity, window.browser)
                if result != "human_waiting":
                    window.result = result
                else:
                    async with self.service.sessions() as session:
                        current = await session.get(WalletOrder, order_id)
                        window.note = FALLBACK.get(current.error)
            window.launching = False
            # Activation is durable before the first pixel/input is exposed to the person.
            if not window.result:
                await self.authorize(window, activate=True)
            return window
        except BaseException:
            window.launching = False
            await self.close(window)
            raise

    async def authorize(self, window, *, activate=False):
        _, _, identity = await self.service.authorize(window.owner, window.order_id)
        if identity != window.identity:
            raise UserError("Підключення змінилося. Відкрий покупку заново з бота.")
        if activate:
            await self.service.authorize(window.owner, window.order_id, activate=True)

    async def detach(self, window):
        # Never release a held mouse button synthetically: that could submit a payment.
        if window.browser and window.browser.pointer_down:
            await self.close(window)
            return
        async with self.lock:
            if window.closing or self.windows.get(window.order_id) is not window:
                return
            window.attached, window.disconnected, window.socket = False, monotonic(), None

    async def close(self, window):
        async with self.lock:
            if self.windows.get(window.order_id) is not window or window.closing or window.launching:
                return
            # Retain the slot while closing the profile to prevent a new launch racing it.
            window.attached = window.closing = True
        try:
            if window.socket and not window.socket.closed:
                await window.socket.close(code=1000, message=b"Session ended")
            if window.manager:
                manager, window.manager = window.manager, None
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(10):
                        await manager.__aexit__(None, None, None)
            await self.service.close(window.owner, window.order_id)
        finally:
            async with self.lock:
                self.windows.pop(window.order_id, None)

    async def expire(self):
        from sqlalchemy import select

        for window in list(self.windows.values()):
            if (monotonic() >= window.deadline or (
                    not window.attached and monotonic() - window.disconnected > 30)):
                await self.close(window)
        async with self.service.sessions() as session:
            expired = list(await session.scalars(select(WalletOrder).where(
                WalletOrder.state.in_(OPEN_STATES), WalletOrder.expires_at <= utcnow(),
            )))
        for order in expired:
            if order.id not in self.windows:
                await self.service.close(order.user_id, order.id)

    async def maintain(self):
        while True:
            try:
                await self.expire()
            except Exception as exc:
                logger.warning("Human checkout cleanup unavailable (%s)", type(exc).__name__)
            await asyncio.sleep(2)


def handoff_app(service, bot_token, *, windows=None):
    if not autobuy.AVAILABLE:
        return soon_app()
    windows = windows or HandoffWindows(service)
    tickets = {}
    pending = 0

    @web.middleware
    async def protect(request, handler):
        try:
            response = await handler(request)
        except web.HTTPException:
            raise
        except (UserError, WalletError) as exc:
            message = str(exc) if isinstance(exc, UserError) else ERRORS.get(str(exc), "Оформлення недоступне.")
            response = web.json_response({"error": message}, status=403)
        except Exception:
            # No raw Playwright/request exception: those can contain cookie data or input text.
            response = web.json_response({"error": "Вікно недоступне. Відкрий його знову з бота."}, status=503)
        return response

    app = web.Application(client_max_size=12288, middlewares=[protect])

    async def headers(request, response):
        response.headers.update({
            "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": (
                "default-src 'none'; script-src 'self' https://telegram.org; style-src 'self'; "
                "img-src 'self' blob:; connect-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'self' https://web.telegram.org https://*.telegram.org"
            ),
        })

    app.on_response_prepare.append(headers)

    def origin(request):
        if request.headers.get("Origin") != service.base_url:
            raise web.HTTPForbidden()

    def order_id(request):
        value = request.match_info["order"]
        if not re.fullmatch(r"[0-9a-f]{32}", value):
            raise web.HTTPNotFound()
        return value

    async def index(request):
        if "delivery" in request.match_info:
            delivery_id(request)
        else:
            order_id(request)
        return web.Response(body=(ASSETS / "index.html").read_bytes(), content_type="text/html")

    def delivery_id(request):
        value = request.match_info["delivery"]
        if not re.fullmatch(r"[0-9a-f]{64}", value):
            raise web.HTTPNotFound()
        return value

    async def asset(request):
        name = request.match_info["name"]
        if name not in {"app.js", "app.css"}:
            raise web.HTTPNotFound()
        return web.Response(body=(ASSETS / name).read_bytes(), content_type=(
            "application/javascript" if name.endswith(".js") else "text/css"
        ))

    async def authenticate(request):
        origin(request)
        payload = await request.json()
        owner = telegram_owner(payload.get("init_data"), bot_token)
        if "delivery" in request.match_info:
            order = await service.request_delivery(owner, delivery_id(request))
            oid = order.id
            if order.state not in OPEN_STATES:
                return web.json_response({"result":order.state, "text":status_text(order)})
        else:
            oid = order_id(request)
        order, account, _ = await service.authorize(owner, oid)
        now = monotonic()
        for key, value in list(tickets.items()):
            if value[2] <= now or value[:2] == (owner, oid):
                tickets.pop(key)
        if len(tickets) >= 128:
            raise web.HTTPTooManyRequests()
        ticket = secrets.token_urlsafe(32)
        tickets[ticket] = (owner, oid, now + 30)
        return web.json_response({
            "ticket": ticket, "region": account.region,
            "stream_path": f"/purchase/{oid}/stream",
            "limit": f"{account.max_total / 100:.2f} {account.currency}",
            "expires_at": order.expires_at.replace(tzinfo=UTC).isoformat(),
            "viewport": VIEWPORT,
            "mode": "balance_first" if service.balance_first else "manual",
        })

    async def socket(request):
        nonlocal pending
        origin(request)
        oid = order_id(request)
        if pending >= 8:
            raise web.HTTPTooManyRequests()
        pending += 1
        pending_slot = True
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=8192)
        window = sender = None
        try:
            await ws.prepare(request)
            first = await ws.receive(timeout=5)
            if first.type != WSMsgType.TEXT:
                raise UserError("Відкрий вікно через Telegram.")
            payload = json.loads(first.data)
            ticket = payload.get("ticket")
            if not isinstance(ticket, str):
                raise web.HTTPForbidden()
            owner, target, expires = tickets.pop(ticket, (None, None, 0))
            if target != oid or monotonic() >= expires:
                raise UserError("Доступ минув. Відкрий вікно знову з Telegram.")
            window = await windows.attach(owner, oid)
            window.socket = ws
            pending -= 1
            pending_slot = False
            if window.result:
                async with service.sessions() as session:
                    current = await session.get(WalletOrder, oid)
                    await ws.send_json({"type":"result", "state":current.state,
                                        "text":status_text(current)})
                await windows.close(window)
                return ws
            if window.note:
                await ws.send_json({"type":"note", "text":window.note})
            window.frames.clear()

            async def frames():
                number = 0
                while not ws.closed:
                    await windows.authorize(window)
                    state, jpeg = await window.browser.frame()
                    number += 1
                    now = monotonic()
                    window.frames = {key: at for key, at in window.frames.items() if now - at < 4}
                    window.frames[number] = now
                    await ws.send_json({"type": "frame", "frame": number, "state": state})
                    await ws.send_bytes(jpeg)
                    await asyncio.sleep(0.4)

            async def safe_frames():
                try:
                    await frames()
                except Exception:
                    await ws.close(code=1011, message=b"View unavailable")

            sender = asyncio.create_task(safe_frames())
            last_seq, input_count, input_since = 0, 0, monotonic()
            async for message in ws:
                if message.type != WSMsgType.TEXT:
                    break
                event = json.loads(message.data)
                sequence = event.get("seq")
                if type(sequence) is not int or sequence <= last_seq:
                    raise ValueError("Repeated input")
                last_seq = sequence
                if event.get("type") == "close":
                    await windows.close(window)
                    break
                stamp = window.frames.get(event.get("frame"), 0)
                if monotonic() - stamp > 4:
                    raise UserError("Зображення застаріло. Відкрий вікно знову.")
                if monotonic() - input_since >= 1:
                    input_count, input_since = 0, monotonic()
                input_count += 1
                if input_count > 80:
                    raise ValueError("Input rate exceeded")
                await windows.authorize(window)
                if event.get("type") == "prepare":
                    await window.browser.prepare(resume=True)
                else:
                    await window.browser.input(event)
        except Exception as exc:
            if ws.prepared and not ws.closed:
                message = str(exc) if isinstance(exc, UserError) else "Сеанс недоступний. Відкрий вікно знову з бота."
                await ws.send_json({"type": "error", "text": message})
                await ws.close(code=1008)
        finally:
            if pending_slot:
                pending -= 1
            if sender:
                sender.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await sender
            if window:
                await windows.detach(window)
        return ws

    async def health(request):
        return web.json_response({"status": "ok", "mode": "human_checkout"})

    app.router.add_get("/health", health)
    app.router.add_get("/buy/{delivery}", index)
    app.router.add_post("/buy/{delivery}/auth", authenticate)
    app.router.add_get("/purchase/{order}", index)
    app.router.add_get("/assets/{name}", asset)
    app.router.add_post("/purchase/{order}/auth", authenticate)
    app.router.add_get("/purchase/{order}/stream", socket)
    return app


@contextlib.asynccontextmanager
async def handoff_server(sessions, settings):
    from .wallet_mode import checkout_mode

    with checkout_mode(sessions, settings.wallet_checkout_mode):
        async with _handoff_server(sessions, settings) as service:
            yield service


@contextlib.asynccontextmanager
async def _handoff_server(sessions, settings):
    if not autobuy.AVAILABLE:
        logger.info("Autobuy disabled: Soon; payment, session and browser workers are not started")
        runner = None
        try:
            if settings.wallet_handoff_url:
                runner = web.AppRunner(soon_app(), access_log=None)
                await runner.setup()
                await web.TCPSite(runner, "127.0.0.1", settings.wallet_handoff_port).start()
            yield None
        finally:
            if runner:
                await runner.cleanup()
        return
    logger.info("Wallet checkout mode=%s public_endpoint=%s", settings.wallet_checkout_mode,
                bool(settings.wallet_handoff_url))
    if settings.wallet_checkout_mode == "bot":
        logger.info("Wallet payment requires Telegram Approve after the full quote")
    if (settings.wallet_checkout_mode == "bot"
            or not settings.wallet_handoff_url and settings.wallet_checkout_mode == "automatic"):
        yield None
        return
    service = HandoffService(sessions, settings.wallet_handoff_url,
                             balance_first=settings.wallet_checkout_mode == "balance_first")
    if not settings.wallet_handoff_url:
        # Mini App mode is still binding while no public endpoint is configured.
        # Never fall back to the automatic payment worker or send a broken link.
        service.register()
        try:
            yield service
        finally:
            service.unregister()
        return
    windows = HandoffWindows(service)
    runner = web.AppRunner(handoff_app(
        service, settings.telegram_bot_token.get_secret_value(), windows=windows,
    ), access_log=None)
    maintenance = None
    try:
        await service.recover()
        await runner.setup()
        await web.TCPSite(runner, "127.0.0.1", settings.wallet_handoff_port).start()
        service.register()
        maintenance = asyncio.create_task(windows.maintain())
        yield service
    finally:
        service.unregister()
        if maintenance:
            maintenance.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await maintenance
        for window in list(windows.windows.values()):
            await windows.close(window)
        await runner.cleanup()
