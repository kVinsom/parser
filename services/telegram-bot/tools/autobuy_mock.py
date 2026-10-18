"""Loopback-only checkout simulator. No Vinted, Telegram, credentials or production DB.

Run with ``python -m tools.autobuy_mock`` from services/telegram-bot.
Tests inject MockBrowser into WalletWorker; production runtime never imports it.
"""

import argparse
import hashlib
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from html import escape
from time import perf_counter
from urllib.parse import urlsplit

from aiohttp import ClientSession, ClientTimeout, web

from vpars_bot.wallet import REGIONS
from vpars_bot.wallet_browser import MONEY, SELECTORS, BrowserCheckout

DELIVERIES = {
    "pickup-a": "Тестове відділення A · Demo Carrier · Test City, 12345",
    "pickup-b": "Тестове відділення B · Demo Carrier · Test City, 12345",
    "home": "Тестовий одержувач · Demo Street 1, Test City, 12345",
}
SCENARIOS = {"success", "decline", "lost_response", "wrong_item", "sold", "ambiguous"}


@dataclass
class MockAccount:
    region: str
    balance: int = 5000
    delivery: str | None = "pickup-a"
    shipping: int = 200
    protection: int = 150
    external_charge: int = 0
    payment_method: str = "Mock balance"
    scenario: str = "success"
    payments: int = 0
    debits: int = 0
    orders: dict = field(default_factory=dict)


def document(body):
    return web.Response(
        text=(
            '<!doctype html><html lang="uk"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            "<title>Autobuy · локальний мок</title><style>"
            "body{font:17px system-ui;max-width:850px;margin:40px auto;padding:0 20px;"
            "background:#f5f6f8;color:#192532}main,section{background:white;padding:24px;"
            "border-radius:12px;margin:18px 0}button,select{font:inherit;padding:10px;"
            "margin:8px 0}label{display:block}dt{font-weight:600}dd{margin:4px 0 12px}"
            "</style><h1>Autobuy · локальний мок</h1>"
            "<p>Тільки вигадані товари, адреси та баланс. Справжніх покупок немає.</p>"
            + body
            + "</html>"
        ),
        content_type="text/html",
    )


class MockCheckout:
    def __init__(self):
        self.accounts = {
            str(user_id): MockAccount(region)
            for user_id, region in zip((101, 202, 303, 404), ("DE", "FR", "IT", "UK"), strict=True)
        }
        self.items = {
            "123": ("Amiri · тестова футболка", 1000),
            "456": ("Stone Island · тестова кофта", 1200),
        }
        self.requests = []
        self.payment_received_times = {}

    def account(self, request):
        key = request.headers.get("X-Mock-Account", request.query.get("account", "101"))
        if key not in self.accounts:
            raise web.HTTPNotFound(text="Unknown mock account")
        return key, self.accounts[key]

    def quote(self, key, item_id):
        account = self.accounts[key]
        price = self.items[item_id][1]
        total = price + account.shipping + account.protection
        delivery = DELIVERIES.get(account.delivery, "")
        raw = {
            "item_id": item_id,
            "currency": REGIONS[account.region][1],
            "item_price": price,
            "shipping": account.shipping,
            "protection": account.protection,
            "total": total,
            "wallet_balance": account.balance,
            "wallet_charge": total - account.external_charge,
            "external_charge": account.external_charge,
            "payment_method": account.payment_method,
            "delivery_key": delivery,
            "account_key": "mock-" + key,
        }
        fingerprint = {k: v for k, v in raw.items() if k != "wallet_balance"}
        raw["checkout_key"] = hashlib.sha256(
            json.dumps(fingerprint, sort_keys=True).encode()
        ).hexdigest()
        return raw

    async def home(self, request):
        key, account = self.account(request)
        choices = "".join(
            f'<option value="{value}" {"selected" if value == account.delivery else ""}>'
            f"{escape(label)}</option>"
            for value, label in DELIVERIES.items()
        )
        accounts = " · ".join(
            f'<a href="/?account={k}">{a.region} / {k}</a>' for k, a in self.accounts.items()
        )
        items = "".join(
            f'<li><a href="/items/{item_id}?account={key}">{escape(title)}</a>'
            f" · {price / 100:.2f} {REGIONS[account.region][1]}</li>"
            for item_id, (title, price) in self.items.items()
        )
        return document(
            f"<p>{accounts}</p><main><h2>Тестовий акаунт {key} · {account.region}</h2>"
            f"<p>Баланс: {account.balance / 100:.2f} {REGIONS[account.region][1]}</p>"
            f'<label>Доставка <select id="delivery">{choices}'
            f'<option value="" {"selected" if account.delivery is None else ""}>'
            'Не вибрано</option></select></label>'
            '<button id="save">Зберегти тестову доставку</button><p id="status"></p>'
            f'<ul>{items}</ul><a href="/api/state?account={key}">Журнал тестових оплат</a>'
            '</main><script>document.querySelector("#save").onclick=async()=>{'
            f'const response=await fetch("/api/config?account={key}",'
            '{method:"POST",headers:{"Content-Type":"application/json"},'
            'body:JSON.stringify({delivery:document.querySelector("#delivery").value||null})});'
            'document.querySelector("#status").textContent=response.ok?"Збережено":"Помилка";'
            "};</script>"
        )

    async def listing(self, request):
        key, _ = self.account(request)
        item_id = request.match_info["item_id"].split("-", 1)[0]
        if item_id not in self.items:
            raise web.HTTPNotFound()
        return document(
            f"<main><h2>{escape(self.items[item_id][0])}</h2>"
            f'<form action="/checkout"><input hidden name="item_id" value="{item_id}">'
            f'<input hidden name="account" value="{key}">'
            '<button id="listing_buy">Відкрити тестове оформлення</button></form></main>'
        )

    async def checkout(self, request):
        key, account = self.account(request)
        item_id = request.query.get("item_id")
        if item_id not in self.items:
            raise web.HTTPNotFound()
        if account.scenario == "sold":
            return document("<main>Цей тестовий товар уже продано.</main>")
        raw = self.quote(key, item_id)
        fields = "".join(
            f'<dt>{name}</dt><dd id="{name}">'
            + escape(f"{value / 100:.2f} {raw['currency']}" if name in MONEY else str(value))
            + "</dd>"
            for name, value in raw.items()
        )
        if account.scenario == "ambiguous":
            fields += '<div id="total">99.00 EUR</div>'
        return document(
            '<main id="checkout_root"><h2>Тестове оформлення</h2><dl>' + fields + "</dl>"
            '<button id="pay">Оплатити тестовим балансом</button></main>'
            '<section id="success_root" hidden><p>Тестову покупку оформлено</p>'
            '<span id="order_id"></span> · <span id="order_item_id"></span></section>'
            '<p id="error"></p><script>document.querySelector("#pay").onclick=async()=>{'
            'document.querySelector("#pay").disabled=true;try{'
            f'const r=await fetch("/api/pay?account={key}",'
            '{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify('
            + json.dumps({"item_id": item_id, "checkout_key": raw["checkout_key"]})
            + ')});if(!r.ok)throw Error("Результат потребує перевірки");'
            "const data=await r.json();"
            'document.querySelector("#order_id").textContent=data.order_id;'
            'document.querySelector("#order_item_id").textContent=data.item_id;'
            'document.querySelector("#success_root").hidden=false;'
            '}catch(e){document.querySelector("#error").textContent=e.message;}};</script>'
        )

    async def config(self, request):
        _, account = self.account(request)
        data = await request.json()
        allowed = {
            "delivery",
            "balance",
            "shipping",
            "protection",
            "external_charge",
            "payment_method",
            "scenario",
        }
        if not isinstance(data, dict) or set(data) - allowed:
            raise web.HTTPBadRequest(text="Unknown configuration")
        for key, value in data.items():
            if key in {"balance", "shipping", "protection", "external_charge"}:
                valid = type(value) is int and 0 <= value <= 100_000_000
            elif key == "delivery":
                valid = value is None or isinstance(value, str) and value in DELIVERIES
            elif key == "scenario":
                valid = isinstance(value, str) and value in SCENARIOS
            else:
                valid = value in ("Mock balance", "Card")
            if not valid:
                raise web.HTTPBadRequest(text="Invalid configuration")
        for key, value in data.items():
            setattr(account, key, value)
        return web.json_response({"mock": True, "configured": True})

    async def pay(self, request):
        key, account = self.account(request)
        self.payment_received_times[key] = perf_counter()
        data = await request.json()
        item_id = data.get("item_id")
        if item_id not in self.items:
            raise web.HTTPBadRequest(text="Unknown item")
        account.payments += 1
        # Independent server ledger: count HTTP attempts separately from actual mock debits.
        if item_id in account.orders:
            return web.json_response(account.orders[item_id])
        raw = self.quote(key, item_id)
        if (
            data.get("checkout_key") != raw["checkout_key"]
            or not account.delivery
            or account.balance < raw["total"]
            or account.external_charge
            or account.payment_method != "Mock balance"
            or account.scenario in {"sold", "decline"}
        ):
            raise web.HTTPConflict(text="Mock payment rejected")
        # No awaits between validation, debit and order recording.
        account.balance -= raw["total"]
        account.debits += 1
        order = {
            "order_id": f"mock-{key}-{item_id}",
            "item_id": item_id,
            "total": raw["total"],
            "delivery": raw["delivery_key"],
            "mock": True,
        }
        account.orders[item_id] = order
        if account.scenario == "lost_response":
            raise web.HTTPServiceUnavailable(text="Mock response lost AFTER debit")
        if account.scenario == "wrong_item":
            return web.json_response({**order, "item_id": "999"})
        return web.json_response(order)

    async def state(self, request):
        key, account = self.account(request)
        return web.json_response(
            {
                "mock": True,
                "account": key,
                "region": account.region,
                "balance": account.balance,
                "delivery": account.delivery,
                "payment_attempts": account.payments,
                "debits": account.debits,
                "orders": list(account.orders.values()),
            }
        )

    def app(self):
        @web.middleware
        async def local_only(request, handler):
            if request.host.split(":", 1)[0] != "127.0.0.1":
                raise web.HTTPForbidden(text="Loopback mock only")
            self.requests.append((request.method, request.path))
            return await handler(request)

        app = web.Application(middlewares=[local_only], client_max_size=8192)
        app.add_routes(
            [
                web.get("/", self.home),
                web.get("/items/{item_id}", self.listing),
                web.get("/checkout", self.checkout),
                web.get("/api/state", self.state),
                web.post("/api/config", self.config),
                web.post("/api/pay", self.pay),
            ]
        )
        return app


@asynccontextmanager
async def serve(mock, port=0):
    runner = web.AppRunner(mock.app(), access_log=None)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", port)
        await site.start()
        yield f"http://127.0.0.1:{runner.addresses[0][1]}"
    finally:
        await runner.cleanup()


class MockBrowser:
    """Test-only transport; actual BrowserCheckout, temporary Chromium context.

    Every browser request is intercepted. Only this literal loopback endpoint is
    contacted, with redirects disabled. No route.continue_()/route.fetch() fallback.
    Production origin checks and selectors remain untouched.
    """

    def __init__(self, browser, endpoint):
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.username
            or parsed.password
            or not parsed.port
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Mock endpoint must be http://127.0.0.1:PORT")
        self.browser, self.endpoint = browser, endpoint
        self.blocked = []

    @asynccontextmanager
    async def adapter(self, account):
        origin = "https://" + REGIONS[account.region][0]
        context = await self.browser.new_context(service_workers="block", accept_downloads=False)
        try:
            async with ClientSession(timeout=ClientTimeout(total=5), trust_env=False) as client:

                async def intercept(route):
                    req = route.request
                    parsed = urlsplit(req.url)
                    if f"{parsed.scheme}://{parsed.netloc}" != origin:
                        self.blocked.append(req.url)
                        await route.abort()
                        return
                    path = parsed.path + ("?" + parsed.query if parsed.query else "")
                    try:
                        async with client.request(
                            req.method,
                            self.endpoint + path,
                            data=req.post_data_buffer,
                            headers={
                                "X-Mock-Account": str(account.user_id),
                                "Content-Type": req.headers.get("content-type", "text/plain"),
                            },
                            allow_redirects=False,
                        ) as response:
                            await route.fulfill(
                                status=response.status,
                                body=await response.read(),
                                content_type=response.headers.get("Content-Type", "text/plain"),
                            )
                    except Exception:
                        await route.abort()

                await context.route("**/*", intercept)
                # Current Playwright supports intercepting sockets too; never allow escape.
                await context.route_web_socket("**/*", lambda socket: socket.close())
                page = await context.new_page()
                contract = {
                    "version": 1,
                    "region": account.region,
                    "expected_account": "mock-" + str(account.user_id),
                    "wallet_label": "Mock balance",
                    "decimal_separator": ".",
                    "selectors": {name: {"css": "#" + name} for name in SELECTORS},
                }
                yield BrowserCheckout(page, account, contract)
        finally:
            await context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be between 1 and 65535")
    web.run_app(MockCheckout().app(), host="127.0.0.1", port=args.port, access_log=None)


if __name__ == "__main__":
    main()
