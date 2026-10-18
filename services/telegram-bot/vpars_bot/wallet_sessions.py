"""Self-service Vinted refresh-key verification; no payment or guessed buyer API."""

import asyncio
import base64
import json
import logging
import math
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit
from uuid import uuid4

from sqlalchemy import and_, or_, select, text

from . import autobuy_availability as autobuy
from .application import locked_user, previous, remember
from .db import Notification, WalletSession, utcnow
from .domain import UserError
from .entitlements import MESSAGES, plan_for
from .session_secrets import TOKEN_PATTERN, session_cipher
from .wallet import ERRORS, REGIONS, WalletError, listing_identity, minor
from .wallet_accounts import wallet_account, wallet_session
from .wallet_signals import signals

logger = logging.getLogger("vpars_bot.wallet_sessions")
SESSION_ERRORS = {
    "BrowserAccountMismatch": "У браузері відкритий інший акаунт Vinted. Використай окремий профіль для цього підключення.",
    "BrowserUnavailable": "Збережений браузер недоступний. Відкрий його на комп’ютері бота й повтори підключення.",
    "BrowserAlreadyBound": "Цей браузер уже закріплений за іншим користувачем або регіоном. Потрібен окремий браузер.",
    "BrowserConnectionInvalid": "Перевір налаштування підключення до локального браузера.",
    "InvalidKey": "Не вдалося прочитати ключ. Потрібне повне значення refresh_token_web із твого Vinted.",
    "GuestKey": "Надіслано ключ гостьової сесії, без входу в акаунт. Ключ не обрізаний. Увійди у Vinted на сайті вибраного регіону, відкрий власний профіль, онови сторінку й скопіюй новий refresh_token_web. Країна сайту не змінює країну акаунту.",
    "WrongKeyType": "Це не refresh_token_web. Скопіюй саме значення refresh_token_web у своєму браузері.",
    "ExpiredKey": "Ключ прострочений. Онови сторінку Vinted і скопіюй свіжий refresh_token_web.",
    "UnsupportedCountry": "Обери підтримуваний регіон підключення: DE, FR, IT або UK.",
    "WrongCountry": "Країна сесії не збігається з обраною. Перевір країну саме акаунту Vinted і почни підключення заново.",
    "RejectedKey": "Vinted відхилив ключ. Він міг бути відкликаний або вже оновлений. Онови Vinted у своєму браузері та надішли свіжий ключ.",
    "SessionChallenge": "Vinted вимагає перевірку входу. Пройди її самостійно у Vinted, потім надішли свіжий ключ.",
    "SessionUnavailable": "Vinted зараз не підтвердив сесію. Спробуй пізніше зі свіжим ключем.",
    "SessionResponseInvalid": "Vinted відповів, але бот не зміг перевірити повернуту сесію. Це не помилка формату надісланого ключа. Викуп вимкнений; повтори підключення зі свіжим ключем.",
    "SessionNotAuthenticated": "Vinted повернув гостьову сесію без входу в акаунт. Увійди у свій акаунт на сайті обраного регіону та надішли свіжий refresh_token_web. Викуп вимкнений.",
    "Interrupted": "Перевірку сесії перервано. Відкрий підключення знову та надішли свіжий ключ.",
    "SettingsChanged": "Налаштування змінилися під час перевірки. Відкрий підключення знову.",
    "AccountInUse": "Цей акаунт Vinted уже підключено до іншого користувача бота.",
}
CHECKOUT_ERRORS = {
    "CheckoutHumanUnavailable": "Сесію збережено. Режим ручної оплати очікує підключення зовнішнього доступу до вікна покупки. Оплата ботом вимкнена.",
    "CheckoutHumanReady": "✅ Сесію збережено. Натисни Buy під товаром, щоб відкрити особисте вікно Vinted. Збережені дані підставляє Vinted; CAPTCHA й оплату підтверджуєш особисто у цьому вікні.",
    "CheckoutUnavailable": "Сесію підключено. Автоматичне оформлення цього акаунту ще не підтримується перевіреним адаптером; викуп вимкнений. Підтверджувати оплату бот не буде.",
    "CheckoutInterrupted": "Перевірку оформлення перервано. Збережену сесію залишено; викуп вимкнений. Натисни «Перевірити оформлення», щоб продовжити без повторного надсилання ключа.",
    "CheckoutPaused": "Викуп вимкнений. Збережену сесію залишено. Коли захочеш продовжити, натисни «Перевірити оформлення».",
    "NoListing": "Очікую доступний лот, який бот надішле тобі в цьому регіоні. Автоматичне підключення повторить перевірку; також можна натиснути «Перевірити оформлення».",
    **ERRORS,
}


async def install_session(user_id, region, cookies, *, background=False):
    from playwright.async_api import async_playwright

    from .wallet_browser import contract_path, profile_lock, wallet_context

    path = contract_path(user_id, region)
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    async with profile_lock(user_id, region), async_playwright() as driver:
        async with wallet_context(
            driver, user_id, region, config, headless=True,
            locale="en-GB", service_workers="block", background=background,
        ) as context:
            await merge_session_cookies(context, region, cookies)


async def merge_session_cookies(context, region, cookies):
    """Refresh auth without erasing this browser's consent/device/challenge cookies."""
    host = REGIONS[region][0]
    domains = {host, host.removeprefix("www.")}
    if not cookies or any(
        cookie.get("domain", "").lstrip(".") not in domains or "url" in cookie
        for cookie in cookies
    ):
        raise WalletError("SessionResponseInvalid")
    access = [c["value"] for c in cookies if c["name"] == "access_token_web"]
    if len(access) != 1:
        raise WalletError("SessionResponseInvalid")
    incoming = claims(access[0])["sub"]
    previous = await context.cookies("https://" + host)
    for cookie in previous:
        if cookie["name"] == "access_token_web":
            try:
                owner = claims(cookie["value"], require_subject=False).get("sub")
            except WalletError:
                raise WalletError("SessionResponseInvalid") from None
            if owner is not None and owner != incoming:
                raise WalletError("BrowserAccountMismatch")
    # Remove only old auth-cookie variants, never the full jar.
    for name in ("access_token_web", "refresh_token_web"):
        await context.clear_cookies(
            name=name, domain=re.compile(r"^\.?(?:" + "|".join(map(re.escape, sorted(domains))) + r")$"),
        )
    existing = {c["name"] for c in previous}
    await context.add_cookies([
        cookie for cookie in cookies
        if cookie["name"] in {"access_token_web", "refresh_token_web"}
        or cookie["name"] not in existing
    ])


async def delivered_candidates(sessions, user_id, region):
    async with sessions() as session:
        return list(await session.scalars(text(
            "SELECT item FROM vpars_monitor.deliveries WHERE user_id=:uid AND state='sent' "
            "AND item->>'item_url' LIKE :origin ORDER BY sent_at DESC LIMIT 50"
        ), {"uid": user_id, "origin": "https://" + REGIONS[region][0] + "/items/%"}))


async def check_checkout(sessions, user_id, identity, *, region=None, candidates=delivered_candidates):
    """Verify an owned delivered listing without Pay; no guessed selectors are generated."""
    from .wallet_browser import browser_adapter, contract_path, load_contract
    from .wallet_handoff import handoff_for

    handoff = handoff_for(sessions)
    if handoff and not handoff.balance_first:
        # Manual checkout needs no guessed payment selectors and does not enable automatic Pay.
        raise WalletError("CheckoutHumanReady" if handoff.base_url else "CheckoutHumanUnavailable")

    async with sessions() as session:
        account = await wallet_account(session, user_id, region)
    if account is None:
        raise WalletError("NotConnected")
    region = account.region
    try:
        contract, digest = load_contract(user_id, account.region)
    except WalletError:
        # Only developers ship inspected per-region DOM contracts. No selectors in user input.
        template = (
            Path(__file__).resolve().parents[1]
            / "config"
            / "wallet-contracts"
            / f"{account.region}.json"
        )
        if not template.is_file():
            raise WalletError("CheckoutUnavailable") from None
        contract = json.loads(template.read_text(encoding="utf-8"))
        contract["expected_account"] = identity
        target = contract_path(user_id, region)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(contract), encoding="utf-8")
        contract, digest = load_contract(user_id, account.region)
    if contract["region"] != account.region or contract["expected_account"] != identity:
        raise WalletError("CheckoutUnavailable")
    items = await candidates(sessions, user_id, account.region)
    order = None
    for raw in items:
        item = json.loads(raw) if isinstance(raw, str) else raw
        try:
            region, item_id = listing_identity(item["item_url"])
            price = minor(item["price"]["amount"])
            if (
                region == account.region
                and item["price"]["currency"] == account.currency
                and price <= account.max_total
            ):
                order = SimpleNamespace(
                    url=item["item_url"],
                    item_id=item_id,
                    listed_price=price,
                    currency=account.currency,
                )
                break
        except (WalletError, KeyError, TypeError):
            continue
    if order is None:
        raise WalletError("NoListing")
    from .wallet_mode import button_checkout

    options = {"background": True} if button_checkout(sessions) else {}
    async with browser_adapter(account, probe=True, **options) as adapter:
        # Use the delivery Vinted has selected for THIS checkout, never choose a point.
        adapter.contract = {**adapter.contract, "delivery_mode": "vinted"}
        quote = await adapter.checkout(order)
        quote.check(order, account)
    if quote.account_key != identity:
        raise WalletError("CheckoutUnavailable")
    if load_contract(user_id, account.region)[1] != digest:
        raise WalletError("ContractMismatch")
    # Display and recheck the actual delivery on every purchase, including confirmation.
    contract["delivery_mode"] = "vinted"
    contract.pop("expected_delivery", None)
    target = contract_path(user_id, region)
    pending = target.with_suffix(".json.tmp")
    pending.write_text(json.dumps(contract), encoding="utf-8")
    pending.replace(target)
    return load_contract(user_id, account.region)[1]


def claims(token, *, require_subject=True):
    try:
        if not isinstance(token, str) or len(token) > 8192 or not TOKEN_PATTERN.fullmatch(token):
            raise ValueError
        data = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        if not isinstance(data, dict) or data.get("iss") not in {
            "vinted-iam-service", "vinted-iam-oauth",
        }:
            raise ValueError
        if type(data.get("exp")) not in (int, float) or not math.isfinite(data["exp"]):
            raise ValueError
        # Must also be representable when the verified expiry is stored.
        datetime.fromtimestamp(data["exp"], UTC)
        subject = data.get("sub")
        if (require_subject or subject is not None) and (
            not isinstance(subject, str) or not subject.isdigit() or len(subject) > 30
        ):
            raise ValueError
        return data
    except (ValueError, TypeError, KeyError, OverflowError, OSError) as exc:
        raise WalletError("InvalidKey") from exc


def validate_key(token, region):
    # Country eligibility is not established by undocumented, unverified JWT `cc`.
    # Validate the chosen storefront, then let that origin verify the session.
    if region not in REGIONS:
        raise WalletError("UnsupportedCountry")
    # A complete public refresh JWT can be issued before login. Missing identity
    # needs a sign-in instruction, not a request to copy the same key again.
    data = claims(token, require_subject=False)
    if data.get("purpose") != "refresh" or data.get("client_id") != "web":
        raise WalletError("WrongKeyType")
    if data["exp"] <= time.time():
        raise WalletError("ExpiredKey")
    if data.get("sub") is None:
        raise WalletError("GuestKey")
    return data


async def verify_session(region, token, persist):
    """Only a fresh successful response from the real Vinted origin verifies a key."""
    from playwright.async_api import TimeoutError as BrowserTimeout
    from playwright.async_api import async_playwright

    expected = validate_key(token, region)
    origin = "https://" + REGIONS[region][0]
    refreshed = asyncio.Event()
    response = None
    stage, outcome = "browser", "SessionUnavailable"
    failure_class = "none"
    access_count = refresh_count = 0
    access_issuer, identity_state = "unread", "unread"
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
        context = await browser.new_context(locale="en-GB", service_workers="block")
        try:
            await context.add_cookies(
                [
                    {
                        "name": "refresh_token_web",
                        "value": token,
                        # Match the scope Vinted uses when rotating this cookie.
                        # A host-only cookie survives beside the rotated domain cookie.
                        "domain": "." + REGIONS[region][0],
                        "path": "/",
                        "httpOnly": True,
                        "secure": True,
                        "sameSite": "Lax",
                    }
                ]
            )
            page = await context.new_page()

            async def navigation_guard(route):
                request = route.request
                if request.is_navigation_request() and request.frame == page.main_frame:
                    p = urlsplit(request.url)
                    if f"{p.scheme}://{p.netloc}" != origin:
                        await route.abort()
                        return
                await route.continue_()

            await page.route("**/*", navigation_guard)

            def received(result):
                nonlocal response
                if (
                    response is None
                    and result.url == origin + "/web/api/auth/refresh"
                    and result.request.method == "POST"
                ):
                    response = result
                    refreshed.set()

            page.on("response", received)
            stage = "refresh_response"
            try:
                await page.goto(origin + "/", wait_until="domcontentloaded", timeout=18000)
                await asyncio.wait_for(refreshed.wait(), 15)
            except (TimeoutError, BrowserTimeout) as exc:
                raise WalletError("SessionUnavailable") from exc
            if response.status in {403, 429}:
                raise WalletError("SessionChallenge")
            if response.status != 200:
                raise WalletError("RejectedKey")
            # The response event confirms status/headers. Authentication is read
            # from the server's cookies, not its body: immediate navigation can
            # discard the body and its completion event after cookies are applied.
            # A 200 alone never authenticates; require the matching access below.
            # Save rotated material immediately; never retry the original refresh key.
            stage = "session_cookies"
            # Preserve the site's session context, including its anon/device cookie.
            # Keeping only the JWTs drops state that the authenticated browser sent
            # during refresh. The origin filter excludes unrelated third-party cookies.
            # The complete selected-origin jar is encrypted by persist(), never logged.
            cookies = await context.cookies(origin)
            access_count = sum(c["name"] == "access_token_web" for c in cookies)
            refresh_count = sum(c["name"] == "refresh_token_web" for c in cookies)
            stage = "session_store"
            await persist(cookies)
            stage = "access_cookie"
            if access_count != 1 or refresh_count != 1:
                raise WalletError("SessionResponseInvalid")
            access = next((c["value"] for c in cookies if c["name"] == "access_token_web"), None)
            try:
                accepted = claims(access, require_subject=False)
            except WalletError:
                # Input syntax already passed validate_key(); this is Vinted's response.
                raise WalletError("SessionResponseInvalid") from None
            access_issuer = accepted["iss"]  # Only fixed, allowlisted issuer names.
            if accepted.get("purpose") != "access":
                raise WalletError("SessionResponseInvalid")
            identity_state = (
                "missing" if accepted.get("sub") is None else
                "same" if accepted["sub"] == expected["sub"] else "different"
            )
            if identity_state == "missing":
                raise WalletError("SessionNotAuthenticated")
            if identity_state != "same":
                raise WalletError("RejectedKey")
            if accepted["exp"] <= time.time():
                raise WalletError("SessionResponseInvalid")
            # This confirms authentication on the selected origin only. Its ability
            # to buy and ship in the region still requires check_checkout().
            outcome = "verified"
            return accepted["sub"], datetime.fromtimestamp(accepted["exp"], UTC)
        except WalletError as exc:
            failure_class = "WalletError"
            outcome = str(exc) if str(exc) in SESSION_ERRORS else "SessionUnavailable"
            raise
        except Exception as exc:
            failure_class = type(exc).__name__
            raise WalletError("SessionUnavailable") from None
        finally:
            # No tokens, subjects, cookies, response bodies, URLs or exception text.
            logger.info(
                "Vinted session region=%s stage=%s http_status=%s access_count=%s "
                "refresh_count=%s access_issuer=%s identity=%s outcome=%s failure_class=%s",
                region, stage, response.status if response is not None else None,
                access_count, refresh_count, access_issuer, identity_state, outcome, failure_class,
            )
            try:
                await context.close()
            finally:
                await browser.close()


class SessionService:
    def __init__(self, sessions, cipher=None):
        self.sessions = sessions
        self._cipher = cipher

    @property
    def cipher(self):
        return self._cipher or session_cipher()

    async def submit(self, user_id, token, operation_id, *, region=None, auto_enable=False):
        autobuy.require_available()
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            replay = await previous(session, user_id, operation_id, "wallet_session")
            if replay is not None:
                return
            if not (await plan_for(session, user)).filters:
                raise UserError(MESSAGES["SubscriptionRequired"])
            account = await wallet_account(session, user_id, region)
            if not account or account.max_total <= 0:
                raise UserError("Спочатку обери країну та збережи ліміт покупки.")
            if account.active_order:
                raise UserError("Спочатку заверши поточну покупку або перевір її результат.")
            try:
                validate_key(token, account.region)
            except WalletError as exc:
                raise UserError(SESSION_ERRORS[str(exc)]) from exc
            row = await wallet_session(session, user_id, region)
            if row and row.state in {"pending", "checking", "probe_pending", "probing"}:
                raise UserError(
                    "Перевірка сесії вже триває. Натисни «Перевірити стан підключення»."
                )
            attempt = uuid4().hex
            if row is None:
                row = WalletSession(user_id=user_id, region=account.region)
                session.add(row)
            row.attempt_id, row.region, row.state = attempt, account.region, "pending"
            row.secret = self.cipher.seal(token, owner=user_id, purpose=f"vinted-key:{attempt}")
            row.error = row.account_key = row.expires_at = None
            row.updated_at = utcnow()
            row.auto_enable = auto_enable
            account.enabled, account.contract_hash = False, None
            remember(session, user_id, operation_id, "wallet_session", {"queued": True})

    async def status(self, user_id, region=None):
        if not autobuy.AVAILABLE:
            return autobuy.SOON
        from .wallet import ERRORS, SESSION_BLOCKS
        from .wallet_mode import button_checkout

        async with self.sessions() as session:
            row = await wallet_session(session, user_id, region)
            if row is None:
                return "Ключ ще не додано."
            if (button_checkout(self.sessions) and row.state in {"connected", "checkout_ready"}
                    and row.error in SESSION_BLOCKS):
                return ERRORS[row.error]
            if row.state in {"probe_pending", "probing"} or (row.state == "checking" and row.account_key):
                return "⏳ Вхід уже підтверджено. Перевіряю оформлення та доставку без оплати."
            if row.state in {"pending", "checking"}:
                return "⏳ Перевіряю ключ безпосередньо у Vinted. Зазвичай це займає до 40 секунд."
            if row.state == "connected":
                from .wallet_handoff import handoff_for

                handoff = handoff_for(self.sessions)
                if handoff and handoff.balance_first:
                    if row.error == "CheckoutPaused":
                        return "Покупки цього регіону вимкнено. Сесію збережено."
                    return ("Сесію збережено. Перед автооплатою потрібна перевірка балансу, "
                            "оформлення та збереженої доставки. За нестачі балансу оплату вибираєш сам."
                            + (" Вікно Buy очікує зовнішнього HTTPS." if not handoff.base_url else ""))
                if handoff:
                    if row.error == "CheckoutPaused":
                        return "Покупки цього регіону вимкнено. Сесію збережено."
                    if not handoff.base_url:
                        return CHECKOUT_ERRORS["CheckoutHumanUnavailable"]
                    return (CHECKOUT_ERRORS["CheckoutHumanReady"] + (
                        "\nПід час попереднього оформлення Vinted показав сторінку захисту. "
                        "Якщо доступна CAPTCHA, пройди її вручну у вікні покупки. "
                        "Якщо Vinted повідомляє про блокування, припини повтори й звернися до підтримки Vinted."
                        if row.error == "CheckoutChallenge" else ""
                    ))
                stale = row.expires_at and row.expires_at.replace(tzinfo=UTC) <= utcnow()
                # This is the expiry of the initially verified access JWT. The site's
                # browser may already have rotated it; it does not prove a revoked key.
                text = (
                    f"Сесію Vinted {row.region} підтверджували раніше. Потрібна повторна перевірка оформлення.\n"
                    if stale else f"✅ Сайт Vinted {row.region} прийняв сесію. Її збережено зашифрованою.\n"
                )
                return text + CHECKOUT_ERRORS.get(
                    row.error, "Оформлення та доставка ще не перевірені. Викуп вимкнений."
                )
            if row.state == "checkout_ready":
                account = await wallet_account(session, user_id, row.region)
                if account and account.enabled:
                    return "✅ Автобай готовий. Доставка — зі збереженого вибору у Vinted."
                return "✅ Сесію, доставку й оформлення перевірено. Можна активувати Buy з погодженим лімітом."
            return SESSION_ERRORS.get(row.error, SESSION_ERRORS["SessionUnavailable"])

    async def probe(self, user_id, region=None, *, auto_enable=False):
        autobuy.require_available()
        async with self.sessions.begin() as session:
            await locked_user(session, user_id)
            row = await wallet_session(session, user_id, region)
            account = await wallet_account(session, user_id, region)
            if (
                not row
                or row.state not in {"connected", "checkout_ready"}
                or not account
                or account.active_order
            ):
                raise UserError("Спочатку підтвердь сесію та заверши поточну покупку, якщо вона є.")
            if row.region != account.region:
                raise UserError(SESSION_ERRORS["WrongCountry"])
            row.state, row.error, row.updated_at = "probe_pending", None, utcnow()
            row.auto_enable = auto_enable
            account.enabled, account.contract_hash = False, None

    async def actions(self, user_id, region=None):
        if not autobuy.AVAILABLE:
            return {}
        from .wallet_browser import load_contract

        async with self.sessions() as session:
            row = await wallet_session(session, user_id, region)
            account = await wallet_account(session, user_id, region)
            from .wallet_handoff import handoff_for

            handoff = handoff_for(self.sessions)
            if handoff and not handoff.balance_first:
                return {"probe": False, "enable": False, "human": bool(
                    row and row.account_key and row.state in {"connected", "checkout_ready"}
                    and row.error == "CheckoutPaused"
                ), "human_connected": bool(row and row.account_key and row.state in {"connected", "checkout_ready"})}
            result = {
                "probe": bool(row and row.state in {"connected", "checkout_ready"}),
                "enable": False,
            }
            if handoff and handoff.balance_first and row and row.error == "CheckoutPaused":
                result.update(probe=False, human=True)
            if row and account and row.state == "checkout_ready" and not account.enabled:
                try:
                    contract, digest = load_contract(user_id, account.region)
                    if digest == account.contract_hash and (contract.get("expected_delivery") or contract.get("delivery_mode") == "vinted"):
                        result.update(enable=True, delivery=contract.get("expected_delivery") or "Збережений вибір у Vinted")
                except WalletError:
                    pass
            return result

    async def resume_human(self, user_id, region):
        autobuy.require_available()
        from .wallet_handoff import handoff_for

        if not handoff_for(self.sessions):
            raise UserError("Цей режим зараз недоступний.")
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            row = await wallet_session(session, user_id, region)
            account = await wallet_account(session, user_id, region)
            if not (await plan_for(session, user)).filters:
                raise UserError(MESSAGES["SubscriptionRequired"])
            if not account or account.active_order or not row or not row.account_key or row.state not in {"connected", "checkout_ready"}:
                raise UserError("Спочатку підтвердь сесію цього регіону.")
            row.state, row.error, row.updated_at = "connected", "CheckoutHumanReady", utcnow()

    async def enable(self, user_id, region=None):
        autobuy.require_available()
        from .wallet_browser import load_contract

        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            account = await wallet_account(session, user_id, region)
            row = await wallet_session(session, user_id, region)
            if not account or not row or row.state != "checkout_ready" or account.active_order:
                raise UserError("Спочатку дочекайся успішної перевірки оформлення.")
            if not (await plan_for(session, user)).filters:
                raise UserError(MESSAGES["SubscriptionRequired"])
            try:
                contract, digest = load_contract(user_id, account.region)
                if (
                    digest != account.contract_hash
                    or contract["region"] != account.region
                    or not (contract.get("expected_delivery") or contract.get("delivery_mode") == "vinted")
                    or contract["expected_account"] != row.account_key
                ):
                    raise WalletError("ContractMismatch")
            except WalletError as exc:
                raise UserError("Потрібна повторна перевірка оформлення.") from exc
            account.enabled = True

    async def disable(self, user_id, region):
        autobuy.require_available()
        async with self.sessions.begin() as session:
            await locked_user(session, user_id)
            account = await wallet_account(session, user_id, region)
            if not account:
                return
            if account.active_order:
                raise UserError("Спочатку заверши поточну покупку або перевір її результат.")
            account.enabled = False
            row = await wallet_session(session, user_id, region)
            if row:
                row.auto_enable = False
                if row.state in {"checking", "probe_pending", "probing"} and row.account_key and row.secret:
                    row.state, row.error, row.updated_at = "connected", "CheckoutPaused", utcnow()
                elif row.state in {"pending", "checking", "probe_pending", "probing"}:
                    row.state, row.error, row.secret = "failed", "Interrupted", None
                else:
                    from .wallet_handoff import handoff_for

                    if handoff_for(self.sessions) and row.state in {"connected", "checkout_ready"}:
                        row.state, row.error, row.updated_at = "connected", "CheckoutPaused", utcnow()


class SessionWorker:
    def __init__(
        self,
        sessions,
        verifier=verify_session,
        cipher=None,
        installer=install_session,
        checkout=check_checkout,
    ):
        self.sessions, self.verifier = sessions, verifier
        self.service = SessionService(sessions, cipher)
        self.installer, self.checkout = installer, checkout

    async def recover(self):
        if not autobuy.AVAILABLE:
            return
        async with self.sessions.begin() as session:
            rows = list(
                await session.scalars(
                    select(WalletSession).where(WalletSession.state.in_(["checking", "probing"]))
                )
            )
            for row in rows:
                if row.account_key and row.secret:
                    # Includes old-version checkout probes stored as "checking".
                    # Never replay the original refresh key or reinstall stale cookies.
                    row.state, row.error = "connected", "CheckoutInterrupted"
                else:
                    row.state, row.error, row.secret = "failed", "Interrupted", None
                row.updated_at = utcnow()

    async def once(self):
        if not autobuy.AVAILABLE:
            return False
        async with self.sessions.begin() as session:
            row = await session.scalar(
                select(WalletSession)
                .where(or_(
                    WalletSession.state.in_(["pending", "probe_pending"]),
                    and_(WalletSession.state == "connected",
                         WalletSession.error == "NoListing",
                         WalletSession.auto_enable.is_(True),
                         WalletSession.updated_at < utcnow() - timedelta(seconds=60)),
                ))
                .order_by(WalletSession.updated_at)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if row is None:
                return False
            probe_only = row.state != "pending"
            previous_error = row.error
            auto_enable = row.auto_enable
            identity, expiry = row.account_key, row.expires_at
            account = await wallet_account(session, row.user_id, row.region)
            expected_limit = account.max_total if account else None
            work_state = "probing" if probe_only else "checking"
            work_started = utcnow()
            row.state, row.updated_at = work_state, work_started
            user_id, attempt, region, encrypted = (
                row.user_id,
                row.attempt_id,
                row.region,
                row.secret,
            )
        persisted = False
        saved_cookies = None

        async def persist(cookies):
            nonlocal persisted, saved_cookies
            async with self.sessions.begin() as session:
                await locked_user(session, user_id)
                current = await wallet_session(session, user_id, region)
                if (current is None or current.attempt_id != attempt or current.state != "checking"
                        or current.updated_at.replace(tzinfo=UTC) != work_started):
                    raise WalletError("SettingsChanged")
                current.secret = self.service.cipher.seal(
                    cookies, owner=user_id, purpose=f"vinted-cookies:{attempt}"
                )
                persisted = True
                saved_cookies = cookies

        error = None
        checkout_error = digest = None
        try:
            if not probe_only:
                token = self.service.cipher.open(
                    encrypted, owner=user_id, purpose=f"vinted-key:{attempt}"
                )
                async with asyncio.timeout(45):
                    identity, expiry = await self.verifier(region, token, persist)
                if not persisted:
                    raise WalletError("SessionUnavailable")
                async with self.sessions() as session:
                    duplicate = await session.scalar(
                        select(WalletSession.user_id)
                        .where(
                            WalletSession.user_id != user_id,
                            WalletSession.region == region,
                            WalletSession.account_key == identity,
                        )
                        .limit(1)
                    )
                if duplicate:
                    raise WalletError("AccountInUse")
                from .wallet_mode import button_checkout

                options = ({"background": button_checkout(self.sessions)}
                           if self.installer is install_session else {})
                await self.installer(user_id, region, saved_cookies, **options)
                # Commit the successful sign-in before the independent checkout step.
                # A restart from here can retain the verified browser and cookie jar.
                async with self.sessions.begin() as session:
                    await locked_user(session, user_id)
                    current = await wallet_session(session, user_id, region)
                    if (current is None or current.attempt_id != attempt or current.state != "checking"
                            or current.updated_at.replace(tzinfo=UTC) != work_started):
                        raise WalletError("SettingsChanged")
                    current.account_key, current.expires_at = identity, expiry
                    work_started = utcnow()
                    current.state, current.updated_at = "probing", work_started
                    work_state = "probing"
            try:
                async with asyncio.timeout(40):
                    digest = await self.checkout(self.sessions, user_id, identity, region=region)
            except Exception as exc:
                checkout_error = (
                    str(exc)
                    if isinstance(exc, WalletError) and str(exc) in CHECKOUT_ERRORS
                    else "CheckoutUnavailable"
                )
        except Exception as exc:
            error = (
                str(exc)
                if isinstance(exc, WalletError) and str(exc) in SESSION_ERRORS
                else "SessionUnavailable"
            )
        async with self.sessions.begin() as session:
            user = await locked_user(session, user_id)
            current = await wallet_session(session, user_id, region)
            account = await wallet_account(session, user_id, region)
            if (current is None or current.attempt_id != attempt or current.state != work_state
                    or current.updated_at.replace(tzinfo=UTC) != work_started):
                return True
            if (
                not account
                or account.region != region
                or account.max_total != expected_limit
                or not (await plan_for(session, user)).filters
            ):
                error = "SettingsChanged"
            current.state = "failed" if error else ("checkout_ready" if digest else "connected")
            current.error = error or checkout_error
            current.updated_at = utcnow()
            if error:
                current.secret = None
            else:
                current.account_key, current.expires_at = identity, expiry
                if digest:
                    account.contract_hash = digest
                    account.enabled = bool(auto_enable)
            if error or digest or checkout_error != previous_error or not probe_only:
                from .wallet_handoff import handoff_for

                handoff = handoff_for(self.sessions)
                purchase_text = (
                    "Після Buy оплата з балансу відбудеться автоматично в межах ліміту. "
                    "Якщо коштів бракує — відкриється ручна оплата."
                    if handoff and handoff.balance_first else
                    "Оплата відбудеться лише після підтвердження суми."
                )
                from .wallet_mode import button_checkout

                if button_checkout(self.sessions):
                    purchase_text = (
                        "Після Buy надішлю повну суму. Approve підтверджує оплату з балансу в межах ліміту. "
                        "Якщо коштів бракує — покупку зупинимо без переходу до оплати."
                    )
                result = (
                    SESSION_ERRORS[error] if error else
                    ("✅ Автобай готовий. Доставка береться з Vinted. Натисни Buy під лотом; "
                     + purchase_text if digest and auto_enable else
                     "✅ Оформлення перевірено. Активуй Buy у меню цього регіону." if digest else
                     CHECKOUT_ERRORS.get(checkout_error, CHECKOUT_ERRORS["CheckoutUnavailable"]))
                )
                session.add(Notification(
                    id=f"wallet-session:{attempt}:{uuid4().hex[:8]}", recipient_id=user_id,
                    kind="text", payload={"text": f"Vinted {region}\n{result}"},
                ))
        signals(self.sessions).notifications.set()
        return True

    async def run(self):
        if not autobuy.AVAILABLE:
            return
        await self.recover()
        while True:
            try:
                if not await self.once():
                    await asyncio.sleep(0.5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Session connection failed (%s)", type(exc).__name__)
                await asyncio.sleep(3)
