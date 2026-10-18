"""Real client browsers with intercepted synthetic checkout; no live purchase."""

from types import SimpleNamespace

import pytest
from test_wallet_browser import browser_page as browser_page
from test_wallet_sessions import key

from vpars_bot.domain import UserError
from vpars_bot.wallet import REGIONS
from vpars_bot.wallet_handoff_browser import VIEWPORT, HumanBrowser


@pytest.fixture
async def mini_app_client(request):
    from playwright.async_api import async_playwright

    engine, device, channel, decoder = request.param
    async with async_playwright() as driver:
        options = {"channel": channel} if channel else {}
        browser = await getattr(driver, engine).launch(headless=True, **options)
        context = await browser.new_context(**driver.devices[device], ignore_https_errors=True)
        if channel == "chrome":
            # Only this synthetic Web container loads a loopback HTTPS origin;
            # the deployed checkout has a public domain, not a LAN address.
            await context.grant_permissions(["local-network-access"], origin="https://web.telegram.org")
        if decoder == "missing":
            await context.add_init_script("window.createImageBitmap = undefined;")
        elif decoder == "rejected":
            await context.add_init_script("window.createImageBitmap = () => Promise.reject(new Error('decode'));")
        try:
            yield context, device, decoder
        finally:
            await browser.close()


@pytest.fixture
def checkout_tls(tmp_path):
    """Disposable loopback certificate: HTTPS iframe/WebSocket test only."""
    import ipaddress
    import ssl
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now-timedelta(minutes=1)).not_valid_after(now+timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.DNSName("localhost"),
            ]), critical=False).sign(key, hashes.SHA256()))
    cert_file, key_file = tmp_path / "checkout.crt", tmp_path / "checkout.key"
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_file.write_bytes(key.private_bytes(serialization.Encoding.PEM,
                                          serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert_file, key_file)
    return context


@pytest.mark.parametrize("mini_app_client", [
    ("chromium", "Pixel 7", None, "native"),
    ("webkit", "iPhone 13", None, "native"),
    ("webkit", "iPhone 13 landscape", None, "missing"),
    ("webkit", "iPhone SE", None, "rejected"),
    ("chromium", "Desktop Chrome", "chrome", "native"),
    ("webkit", "Desktop Safari", None, "native"),
], indirect=True)
@pytest.mark.parametrize("flow", ["manual", "balance_paid", "balance_short"])
async def test_full_mini_app_shows_same_checkout_and_only_user_can_pay(browser_page, db, registry, mini_app_client, checkout_tls, flow):
    import asyncio
    import json
    from contextlib import asynccontextmanager
    from pathlib import Path

    from aiohttp.test_utils import TestServer
    from test_wallet_handoff import TOKEN, delivery_reference, init_data, setup

    from vpars_bot.wallet_handoff_web import HandoffWindows, handoff_app

    service = await setup(db[1], registry)
    if flow != "manual":
        from dataclasses import replace

        from sqlalchemy import select
        from test_wallet import good_quote
        from test_wallet_balance import BalanceSite

        from vpars_bot.db import WalletAccount

        service.balance_first = True
        async with db[1].begin() as session:
            for account in await session.scalars(select(WalletAccount)):
                account.enabled, account.contract_hash = True, "a" * 64
        site = BalanceSite(db[1], replace(good_quote(), item_id="1002", account_key="101",
                                         wallet_balance=0 if flow == "balance_short" else 5000))
    browser = await browser_fixture(browser_page, "DE", 101)
    if flow != "manual":
        browser.balance_adapter = site.balance_adapter
    inputs = []
    original_input = browser.input
    async def track_input(event):
        inputs.append(event)
        await original_input(event)
    browser.input = track_input
    await browser_page.locator('[data-testid="item-buy-button"]').evaluate('''button => {
      button.onclick = () => {
        window.buys=(window.buys||0)+1;history.pushState({},'', '/checkout');
        document.body.innerHTML=`<style>body{font:16px Arial;color:#173e3a;padding:18px}h2{margin-top:20px}section{border:1px solid #ddd;padding:18px;margin:18px 0;border-radius:8px}button{background:#218658;color:white;padding:18px;border:0;border-radius:8px;width:100%;font-size:18px}</style>
        <h2>Vinted · Test checkout</h2><section><b>Stone Island · Test item</b><p>Item: 10.00 €</p><p>Shipping: 2.00 €</p><p>Protection: 1.50 €</p><h2>Total: 13.50 €</h2></section><section>Saved delivery · TEST FIXTURE</section><input id="contact" aria-label="Test contact" style="font-size:16px;width:100%;margin-bottom:12px"><button id="pay" onclick="window.pays=(window.pays||0)+1;this.textContent='Test payment confirmed'">Pay 13.50 €</button>`;
      };
    }''')

    @asynccontextmanager
    async def launch(*args):
        await browser.prepare()
        yield browser

    windows = HandoffWindows(service, factory=launch)
    server = TestServer(handoff_app(service, TOKEN, windows=windows))
    await server.start_server(ssl=checkout_tls)
    async with server:
        service.base_url = str(server.make_url("")).rstrip("/")
        context, device, decoder = mini_app_client
        checkout_url = service.base_url + "/buy/" + delivery_reference(101, 2)
        embedded = device.startswith("Desktop")
        async def guard(route):
            if route.request.url == "https://web.telegram.org/test-checkout":
                # Synthetic Telegram Web container, no real Telegram session.
                await route.fulfill(content_type="text/html", body=(
                    '<!doctype html><title>Telegram Web fixture</title><style>'
                    'body{margin:0}iframe{border:0;width:100%;height:100vh}</style>'
                    f'<iframe name="checkout" src="{checkout_url}"></iframe>'
                ))
            elif route.request.url.startswith("https://telegram.org/js/"):
                await route.fulfill(content_type="application/javascript", body="window.Telegram={WebApp:{initData:" + json.dumps(init_data()) + ",ready(){},expand(){},close(){}}};")
            elif route.request.url.startswith((service.base_url, "blob:" + service.base_url + "/")):
                await route.continue_()
            else:
                await route.abort()
        await context.route("**/*", guard)
        page = container = await context.new_page()
        errors, diagnostics = [], []
        page.on("pageerror", lambda error: errors.append(type(error).__name__))
        page.on("console", lambda message: diagnostics.append(message.text) if message.type == "error" else None)
        page.on("requestfailed", lambda request: diagnostics.append((request.url.split(":", 1)[0], request.failure)))
        try:
            # This is the URL placed directly on a delivered card's Buy button.
            if embedded:
                await container.goto("https://web.telegram.org/test-checkout")
                page = container.frame(name="checkout")
                assert page is not None, (diagnostics, [frame.url for frame in container.frames])
            else:
                await page.goto(checkout_url)
            if flow == "balance_paid":
                for _ in range(100):
                    if "Покупку оформлено" in await page.locator("#placeholder").inner_text():
                        break
                    await asyncio.sleep(0.05)
                assert "Покупку оформлено" in await page.locator("#placeholder").inner_text()
                assert not await page.locator("#screen").is_visible()
                assert not await page.locator("#controls").is_visible()
                assert site.payments == 1 and not inputs
                assert not errors
                return
            for _ in range(100):
                if await page.locator("#screen").is_visible():
                    break
                await asyncio.sleep(0.05)
            assert await page.locator("#screen").is_visible(), (errors, diagnostics, await page.locator("#status").all_text_contents())
            assert await browser_page.evaluate("window.buys") == 1
            assert await browser_page.evaluate("window.pays||0") == 0
            assert "доставку" in await page.locator("#status").inner_text()
            if flow == "balance_short":
                assert "бракує коштів" in await page.locator("#payment-notice").inner_text()
                assert site.payments == 0
            assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            target = Path(".artifacts") / f"handoff-preview-{device.replace(' ', '-')}-{decoder}-{flow}.png"
            await container.screenshot(path=str(target), full_page=True)

            async def remote_click(selector):
                button = await browser_page.locator(selector).bounding_box()
                canvas = await page.locator("#screen").bounding_box()
                position = {"x": (button["x"]+10)*canvas["width"]/420,
                            "y": (button["y"]+10)*canvas["height"]/820}
                if await page.evaluate("navigator.maxTouchPoints > 0"):
                    await page.locator("#screen").tap(position=position)
                else:
                    await page.locator("#screen").click(position=position)

            await remote_click("#contact")
            await page.locator("summary").click()
            await page.locator("#text").fill("Teststraße 12, Київ")
            await page.locator("#typing button").click()
            for _ in range(40):
                if await browser_page.locator("#contact").input_value() == "Teststraße 12, Київ":
                    break
                await asyncio.sleep(0.05)
            assert await browser_page.locator("#contact").input_value() == "Teststraße 12, Київ"
            assert await page.locator("#text").input_value() == ""
            await page.locator("summary").click()

            # PointerEvent dispatch tests the swipe/cancel state machine in each
            # engine. Native touch taps are exercised separately above/below.
            before = len(inputs)
            payment = await browser_page.locator("#pay").bounding_box()
            await page.locator("#screen").evaluate('''(canvas, payment) => {
              const original = canvas.setPointerCapture;
              canvas.setPointerCapture = () => {};
              const r = canvas.getBoundingClientRect();
              const x = r.left+(payment.x+10)*r.width/420;
              const y = r.top+(payment.y+10)*r.height/820;
              for (const [type,dy] of [['pointerdown',0],['pointermove',-50],['pointerup',-50]])
                canvas.dispatchEvent(new PointerEvent(type,{pointerId:7,pointerType:'touch',isPrimary:true,button:0,clientX:x,clientY:y+dy}));
              canvas.setPointerCapture = original;
            }''', payment)
            await asyncio.sleep(0.1)
            assert any(e["type"] == "scroll" for e in inputs[before:])
            assert not any(e["type"] in {"down", "up"} for e in inputs[before:])
            assert await browser_page.evaluate("window.pays||0") == 0
            button = await browser_page.locator("#pay").bounding_box()
            await remote_click("#pay")
            for _ in range(30):
                if await browser_page.evaluate("window.pays||0"):
                    break
                await asyncio.sleep(0.05)
            assert await browser_page.evaluate("window.pays") == 1, (inputs, button, await page.locator("#status").inner_text())
            assert not errors
            # Cancelling a manually enabled drag sends no synthetic mouseup.
            await page.locator("summary").click()
            await page.locator("#drag-mode").click()
            assert await page.locator("#drag-mode").get_attribute("aria-pressed") == "true"
            before = len(inputs)
            await page.locator("#screen").evaluate('''canvas => {
              const original = canvas.setPointerCapture;
              canvas.setPointerCapture = () => {};
              const r=canvas.getBoundingClientRect();
              canvas.dispatchEvent(new PointerEvent('pointerdown',{pointerId:9,pointerType:'touch',isPrimary:true,button:0,clientX:r.left+50,clientY:r.top+100}));
              canvas.dispatchEvent(new PointerEvent('pointercancel',{pointerId:9}));
              canvas.setPointerCapture=original;
            }''')
            await page.locator("#screen").wait_for(state="hidden")
            assert all(e["type"] != "up" for e in inputs[before:])
            assert await browser_page.evaluate("window.pays") == 1
            await page.locator("#close").click()
        finally:
            for window in list(windows.windows.values()):
                await windows.close(window)


async def browser_fixture(page, region, uid, *, challenge=False):
    origin = "https://" + REGIONS[region][0]
    await page.set_viewport_size(VIEWPORT)
    await page.context.add_cookies([{"name": "access_token_web", "value": key(subject=str(uid)), "url": origin}])
    await page.goto(origin + "/items/123-stone-island")
    await page.set_content('''<button data-testid="item-buy-button" onclick="window.buys=(window.buys||0)+1;">Buy</button>
      <button id="pay" style="position:absolute;top:100px;left:10px" onclick="window.pays=(window.pays||0)+1">Pay</button>''')
    if challenge:
        await page.evaluate('''() => {const f=document.createElement('iframe');f.src='https://geo.captcha-delivery.com/captcha/';document.body.append(f)}''')
    return HumanBrowser(page, SimpleNamespace(url=origin + "/items/123-stone-island"), str(uid))


@pytest.mark.parametrize("region", list(REGIONS))
@pytest.mark.parametrize("uid", [101, 202])
async def test_prepare_clicks_only_listing_buy_once_and_never_pay(browser_page, region, uid):
    browser = await browser_fixture(browser_page, region, uid)
    await browser.prepare()
    await browser.prepare()
    assert await browser_page.evaluate("window.buys") == 1
    assert await browser_page.evaluate("window.pays||0") == 0
    state, frame = await browser.frame()
    assert state == "vinted" and frame[:2] == b"\xff\xd8"


async def test_captcha_is_only_shown_no_challenge_or_payment_click(browser_page):
    browser = await browser_fixture(browser_page, "DE", 101, challenge=True)
    await browser.prepare()
    assert await browser.state() == "challenge"
    assert await browser_page.evaluate("window.buys||0") == 0
    assert await browser_page.evaluate("window.pays||0") == 0


async def test_after_manual_captcha_only_explicit_resume_can_retry_listing_buy(browser_page):
    browser = await browser_fixture(browser_page, "DE", 101)
    await browser.prepare()
    await browser_page.evaluate('''() => {const f=document.createElement('iframe');f.src='https://geo.captcha-delivery.com/captcha/';document.body.append(f)}''')
    assert await browser.state() == "challenge"
    await browser.prepare(resume=True)
    assert await browser_page.evaluate("window.buys") == 1
    # Simulate the fixture's human-completed challenge; never performed on live Vinted.
    await browser_page.locator("iframe").evaluate("frame => frame.remove()")
    await browser.prepare()
    assert await browser_page.evaluate("window.buys") == 1
    await browser.prepare(resume=True)
    await browser.prepare(resume=True)
    assert await browser_page.evaluate("window.buys") == 2
    assert await browser_page.evaluate("window.pays||0") == 0


async def test_wrong_identity_never_prepares_purchase(browser_page):
    browser = await browser_fixture(browser_page, "DE", 101)
    browser.identity = "202"
    with pytest.raises(UserError):
        await browser.prepare()
    assert await browser_page.evaluate("window.buys||0") == 0


@pytest.mark.parametrize("react_placeholder", [False, True])
async def test_listing_waits_for_hydration_and_never_invokes_react_handler(browser_page, react_placeholder):
    browser = await browser_fixture(browser_page, "DE", 101)
    await browser_page.locator('[data-testid="item-buy-button"]').evaluate('''(button, placeholder) => {
      button.onclick = null;
      if (placeholder) {
        button.__reactFiber$fixture = {};
        button.__reactProps$fixture = {};
        button.onclick = () => {};
      }
      setTimeout(() => {
        button.__reactProps$fixture = {onClick:() => {throw Error('must not invoke internals')}};
        button.addEventListener('click', () => window.buys=(window.buys||0)+1);
      }, 150);
    }''', react_placeholder)
    await browser.prepare()
    await browser.prepare()
    assert await browser_page.evaluate("window.buys") == 1
    assert await browser_page.evaluate("window.pays||0") == 0


async def test_checkout_loading_finishes_before_balance_can_be_read(browser_page):
    browser = await browser_fixture(browser_page, "DE", 101)
    await browser_page.locator('[data-testid="item-buy-button"]').evaluate('''button => {
      button.onclick = () => {
        window.buys=(window.buys||0)+1;
        history.pushState({}, '', '/checkout');
        document.body.innerHTML='<div data-testid="single-checkout-loader">Loading</div>';
        setTimeout(() => document.body.innerHTML='<div data-testid="checkout">Ready</div>', 150);
      };
    }''')
    await browser.prepare()
    await browser.wait_for_checkout()
    assert await browser_page.locator('[data-testid="checkout"]').is_visible()
    assert await browser_page.evaluate("window.buys") == 1


async def test_user_gesture_is_the_only_way_to_click_payment(browser_page):
    browser = await browser_fixture(browser_page, "DE", 101)
    await browser.prepare()
    rect = await browser_page.locator("#pay").bounding_box()
    point = {"x": rect["x"] + 5, "y": rect["y"] + 5}
    await browser.input({"type": "down", **point})
    assert await browser_page.evaluate("window.pays||0") == 0
    await browser.input({"type": "up", **point})
    assert await browser_page.evaluate("window.pays") == 1
    await browser.input({"type": "up", **point})
    assert await browser_page.evaluate("window.pays") == 1


@pytest.mark.parametrize("event", [{"type": "script", "value": "alert(1)"},
    {"type": "key", "value": "Control+l"}, {"type": "down", "x": float("nan"), "y": 1},
    {"type": "down", "x": -1, "y": 1}, {"type": "scroll", "delta": 999999},
    {"type": "text", "value": "line\nline"},
    {"type": "scroll", "delta": 100, "x": -1, "y": 0},
    {"type": "scroll", "delta": 100, "x": 10}])
async def test_remote_input_has_no_script_navigation_or_unbounded_parameters(browser_page, event):
    browser = await browser_fixture(browser_page, "DE", 101)
    with pytest.raises(ValueError):
        await browser.input(event)
    assert await browser_page.evaluate("window.pays||0") == 0


async def test_touch_scroll_targets_nested_panel_without_clicking(browser_page):
    browser = await browser_fixture(browser_page, "DE", 101)
    await browser_page.set_content('''<div id="panel" style="position:absolute;left:100px;top:100px;width:200px;height:200px;overflow:auto">
      <button onclick="window.pays=1">Pay</button><div style="height:1500px">Fixture</div></div>''')
    await browser.input({"type":"scroll", "delta":300, "x":180, "y":180})
    await browser_page.wait_for_function("document.querySelector('#panel').scrollTop > 0")
    assert await browser_page.evaluate("window.pays||0") == 0

