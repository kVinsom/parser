"""Local dedicated Vinted browser; no private API guesses or shared scraper cookies.

The checkout selector contract must be inspected and probed against the connected
account. No unverified default contract is shipped. A changed/missing field fails closed.
"""

import asyncio
import hashlib
import json
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from . import autobuy_availability as autobuy
from .wallet import REGIONS, Quote, WalletError, listing_identity, minor
from .wallet_address import ADDRESS_STATE, address_requirement
from .wallet_navigation import background_navigation_guard, navigation_guard
from .wallet_phone import PHONE_STATE, phone_requirement

ROOT = Path(__file__).resolve().parents[1]
FIELDS = {
    "item_id",
    "currency",
    "item_price",
    "shipping",
    "protection",
    "total",
    "wallet_balance",
    "wallet_charge",
    "external_charge",
    "payment_method",
    "delivery_key",
    "account_key",
    "checkout_key",
}
MONEY = {
    "item_price",
    "shipping",
    "protection",
    "total",
    "wallet_balance",
    "wallet_charge",
    "external_charge",
}
SELECTORS = FIELDS | {
    "listing_buy",
    "checkout_root",
    "pay",
    "success_root",
    "order_id",
    "order_item_id",
}


def profile_dir(user_id, region=None):
    if type(user_id) is not int or user_id <= 0:
        raise WalletError("NotConnected")
    if region is not None and region not in REGIONS:
        raise WalletError("NotConnected")
    # Keep an existing profile in place only for the country in its old contract.
    # Never copy or share its cookies with another country.
    suffix = "" if region is None or legacy_region(user_id) == region else f"-{region}"
    return ROOT / ".wallet-profiles" / f"{user_id}{suffix}"


def legacy_region(user_id):
    try:
        data = json.loads((ROOT / ".wallet-contracts" / f"{user_id}.json").read_text("utf-8"))
        return data.get("region") if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def contract_path(user_id, region=None):
    profile_dir(user_id, region)  # Validate IDs and countries before constructing a path.
    suffix = "" if region is None or legacy_region(user_id) == region else f"-{region}"
    return ROOT / ".wallet-contracts" / f"{user_id}{suffix}.json"


def browser_options(contract):
    cdp_endpoint(contract)  # Validate connection settings even before a browser is opened.
    channel = contract.get("browser_channel")
    if channel is None or channel == "chromium":
        return {}
    if isinstance(channel, str) and channel in {"chrome", "msedge"}:
        return {"channel": channel}
    raise WalletError("ContractMismatch")


def cdp_endpoint(config):
    endpoint = config.get("browser_cdp_url")
    if endpoint is None:
        return None
    # A debugging endpoint grants access to the whole browser. Only an explicit,
    # local, dedicated browser is supported; no credentials, remote hosts or redirects.
    if not isinstance(endpoint, str) or not re.fullmatch(
        r"http://127\.0\.0\.1:[0-9]{1,5}/?", endpoint
    ):
        raise WalletError("BrowserConnectionInvalid")
    port = int(endpoint.rstrip("/").rsplit(":", 1)[1])
    if port is None or not 1024 <= port <= 65535:
        raise WalletError("BrowserConnectionInvalid")
    return f"http://127.0.0.1:{port}"


def load_contract(user_id, region=None):
    try:
        data = json.loads(contract_path(user_id, region).read_text(encoding="utf-8"))
        if (
            data.get("version") != 1
            or data.get("region") not in REGIONS
            or (region is not None and data.get("region") != region)
            or set(data.get("selectors", {})) != SELECTORS
            or not data.get("wallet_label")
            or not data.get("expected_account")
            or data.get("decimal_separator") not in {".", ","}
            or data.get("delivery_mode") not in {None, "vinted"}
        ):
            raise ValueError
        for value in data["selectors"].values():
            if (
                not isinstance(value, dict)
                or set(value) - {"css", "attribute"}
                or not isinstance(value.get("css"), str)
                or not value["css"].strip()
                or len(value["css"]) > 500
            ):
                raise ValueError
            attr = value.get("attribute")
            if attr is not None and not re.fullmatch(r"(?:data-[a-z0-9-]+|content|value)", attr):
                raise ValueError
        browser_options(data)
        delivery = data.get("expected_delivery")
        if delivery is not None and (
            not isinstance(delivery, str) or not delivery.strip() or len(delivery) > 500
        ):
            raise ValueError
        digest = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        return data, digest
    except (OSError, ValueError, TypeError) as exc:
        raise WalletError("ContractMismatch") from exc


@asynccontextmanager
async def profile_lock(user_id, region=None, *, timeout=10):
    """Cross-process guard for a warm worker versus manual login/delivery/probe."""
    directory = profile_dir(user_id, region)
    async with directory_lock(directory, timeout=timeout):
        yield


@asynccontextmanager
async def directory_lock(directory, *, timeout=10):
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / ".autobuy.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        acquired = False
        deadline = asyncio.get_running_loop().time() + timeout
        try:
            while not acquired:
                handle.seek(0)
                try:
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                except OSError:
                    if asyncio.get_running_loop().time() >= deadline:
                        raise WalletError("ProfileBusy") from None
                    await asyncio.sleep(0.1)
            yield
        finally:
            if acquired:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@asynccontextmanager
async def wallet_context(driver, user_id, region, config, *, background=False, **options):
    """Reuse a dedicated local browser, or launch the existing persistent profile.

    Caller holds profile_lock. CDP connections never close the owner's context or
    existing tabs. A persistent endpoint binding prevents cross-user/country reuse.
    """
    endpoint = cdp_endpoint(config)
    if background and endpoint:
        async with background_context(driver, user_id, region, config, **options) as context:
            yield context
        return
    if endpoint is None:
        context = await driver.chromium.launch_persistent_context(
            str(profile_dir(user_id, region)), chromium_sandbox=True,
            accept_downloads=False, **options, **browser_options(config),
        )
        try:
            yield context
        finally:
            await context.close()
        return

    profile_dir(user_id, region)  # Validate the owner before writing a binding.
    directory = ROOT / ".wallet-profiles" / ".cdp-bindings" / str(urlsplit(endpoint).port)
    async with directory_lock(directory):
        binding = directory / "owner.json"
        owner = {"user_id": user_id, "region": region}
        if binding.exists():
            try:
                current = json.loads(binding.read_text("utf-8"))
            except (OSError, ValueError):
                raise WalletError("BrowserConnectionInvalid") from None
            if current != owner:
                raise WalletError("BrowserAlreadyBound")
        # no_defaults preserves the existing browser's locale/media/focus settings.
        # Driver shutdown disconnects CDP; Browser.close/Context.close are not used.
        try:
            browser = await driver.chromium.connect_over_cdp(
                endpoint, timeout=10_000, no_defaults=True,
            )
        except Exception:
            raise WalletError("BrowserUnavailable") from None
        if len(browser.contexts) != 1:
            raise WalletError("BrowserConnectionInvalid")
        if not binding.exists():
            binding.write_text(json.dumps(owner), encoding="utf-8")
        yield browser.contexts[0]


@asynccontextmanager
async def background_context(driver, user_id, region, config, **options):
    """Use the dedicated login profile headlessly; never open/attach a visible window.

    Only profiles made by our browser launcher and already bound to this owner are
    accepted. An open manual browser keeps ownership; do not close or copy it.
    """
    profile_dir(user_id, region)
    endpoint = cdp_endpoint(config)
    channel = config.get("browser_channel")
    if channel not in {"chrome", "msedge"}:
        raise WalletError("BrowserConnectionInvalid")
    port = urlsplit(endpoint).port
    bindings = ROOT / ".wallet-profiles" / ".cdp-bindings" / str(port)
    async with directory_lock(bindings):
        try:
            owner = json.loads((bindings / "owner.json").read_text("utf-8"))
        except (OSError, ValueError):
            raise WalletError("BrowserConnectionInvalid") from None
        if owner != {"user_id": user_id, "region": region}:
            raise WalletError("BrowserAlreadyBound")
        try:
            _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1)
        except OSError:
            pass
        except TimeoutError:
            raise WalletError("BrowserUnavailable") from None
        else:
            writer.close()
            await writer.wait_closed()
            raise WalletError("ProfileBusy")
        directory = ROOT / ".wallet-profiles" / f"cdp-{channel}-{user_id}-{region}"
        legacy = ROOT / ".wallet-profiles" / f"cdp-{user_id}-{region}"
        if channel == "msedge" and not directory.exists() and legacy.exists():
            directory = legacy
        if (not directory.is_dir()
                or not directory.resolve().is_relative_to((ROOT / ".wallet-profiles").resolve())):
            raise WalletError("BrowserConnectionInvalid")
        # This profile was created by the owner's normal Chrome/Edge. Keep its
        # language and service workers when reopening it for background checkout.
        # The general managed-profile defaults must not overwrite that environment.
        options.pop("locale", None)
        options.pop("service_workers", None)
        options["headless"] = True
        try:
            context = await driver.chromium.launch_persistent_context(
                str(directory), chromium_sandbox=True, accept_downloads=False,
                **options, **browser_options(config),
            )
        except Exception:
            raise WalletError("BrowserUnavailable") from None
        try:
            yield context
        finally:
            await context.close()


@asynccontextmanager
async def wallet_page(context, config, *, viewport=None, preserve_challenge=False):
    """Only our new tab is owned; attaching must never close the owner's tabs."""
    external = cdp_endpoint(config) is not None
    page = await context.new_page()
    keep = False
    try:
        if external:
            # Existing Chrome may throttle a background tab's animation frames,
            # stalling Playwright's stability check before the first click.
            await page.bring_to_front()
        else:
            for previous in context.pages:
                if previous != page:
                    await previous.close()
        if viewport is not None:
            await page.set_viewport_size(viewport)
        yield page
    except WalletError as exc:
        keep = external and preserve_challenge and str(exc) == "CheckoutChallenge"
        raise
    finally:
        if external and not page.is_closed():
            if keep:
                # Leave the actual challenge tab available to the local person.
                await page.unroute_all(behavior="wait")
                page.remove_listener("popup", close_popup)
                page.remove_listener("dialog", dismiss_dialog)
            else:
                await page.close()


def close_popup(popup):
    asyncio.create_task(popup.close())


def dismiss_dialog(dialog):
    asyncio.create_task(dialog.dismiss())


# Read only visible, unambiguous elements from the checkout, in one browser task.
# A payment click uses the SAME snapshot and compares every field with the quote.
# There is no first() fallback, arbitrary script from config, or guessed money field.
SNAPSHOT = """({selectors, fields, expected, origin}) => {
  if (location.origin !== origin || location.pathname.replace(/\\/$/, '') !== '/checkout')
    throw Error('checkout_origin');
  const addressRequirement = (""" + ADDRESS_STATE + """)();
  if (addressRequirement) throw Error('checkout_address_required');
  const phoneRequirement = (""" + PHONE_STATE + """)();
  if (phoneRequirement) throw Error('checkout_phone_required');
  const visible = e => !!(e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
  if ([...document.querySelectorAll('[data-testid="checkout-generic-error-modal"]')].some(visible))
    throw Error('checkout_rejected');
  const one = (root, spec) => {
    const all = [...root.querySelectorAll(spec.css)].filter(visible);
    if (all.length !== 1) throw Error('ambiguous_checkout');
    return all[0];
  };
  const root = one(document, selectors.checkout_root);
  const values = {};
  for (const key of fields) {
    const spec = selectors[key], element = one(root, spec);
    const value = spec.attribute ? element.getAttribute(spec.attribute) : element.textContent;
    // An empty delivery choice is an actionable setup issue. Quote.check() rejects
    // it as ManualAction; all payment/identity fields still fail the DOM contract.
    if (typeof value !== 'string' || (!value.trim() && key !== 'delivery_key'))
      throw Error('missing_checkout');
    values[key] = value.trim();
  }
  if (expected) {
    for (const key of fields) if (expected[key] !== values[key]) throw Error('changed_checkout');
    const pay = one(root, selectors.pay);
    if (pay.tagName !== 'BUTTON' || pay.disabled || pay.getAttribute('aria-disabled') === 'true')
      throw Error('payment_unavailable');
    pay.click();
  }
  return values;
}"""


def money_text(raw, currency, separator):
    symbol = {"EUR": "€", "GBP": "£"}[currency]
    value = raw.strip().replace("\u00a0", " ")
    # Require explicit currency per field; do not infer from a neighbouring unrelated price.
    match = re.fullmatch(
        rf"(?:{re.escape(symbol)}|{currency})\s*([0-9]+(?:{re.escape(separator)}[0-9]{{2}})?)"
        rf"|([0-9]+(?:{re.escape(separator)}[0-9]{{2}})?)\s*(?:{re.escape(symbol)}|{currency})",
        value,
    )
    if not match:
        raise WalletError("ContractMismatch")
    return minor((match[1] or match[2]).replace(separator, "."))


async def listing_buy_ready(page, css):
    """Wait for the observed Vinted React click handler, not just server-rendered HTML.

    Inspect readiness only; never call the handler or mutate framework internals.
    Native onclick also supports ordinary HTML pages. A changed implementation
    times out without dispatching an uncertain click.
    """
    from playwright.async_api import TimeoutError as BrowserTimeout

    try:
        await page.wait_for_function("""css => {
          const matches = [...document.querySelectorAll(css)].filter(e =>
            e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
          if (matches.length !== 1) return false;
          const e = matches[0];
          const keys = Object.keys(e);
          const react = keys.some(k => k.startsWith('__reactProps$') || k.startsWith('__reactFiber$'));
          // React may install a native noop before attaching the real handler.
          // That placeholder is not evidence that the Buy action is ready.
          const ready = react ? keys.some(k =>
            k.startsWith('__reactProps$') && typeof e[k]?.onClick === 'function')
            : typeof e.onclick === 'function' || (e.type === 'submit' && !!e.form);
          return e.tagName === 'BUTTON' && !e.disabled && e.getAttribute('aria-disabled') !== 'true'
            && ready;
        }""", arg=css, timeout=8000)
        return True
    except BrowserTimeout:
        return False


class BrowserCheckout:
    def __init__(self, page, account, contract, *, prepared_item=None):
        self.page, self.account, self.contract = page, account, contract
        self.origin = "https://" + REGIONS[account.region][0]
        self.raw = None
        self.submitted = False
        self.prepared_item = prepared_item

    def trusted(self):
        p = urlsplit(self.page.url)
        if f"{p.scheme}://{p.netloc}" != self.origin:
            raise WalletError("ManualAction")

    async def require_no_challenge(self):
        # Recognize the challenge frame observed during real checkout. Never click,
        # inspect its puzzle, solve it or infer CAPTCHA from an arbitrary HTTP 403.
        for frame in await self.page.locator("iframe[src]").all():
            source = await frame.get_attribute("src")
            try:
                url = urlsplit(source or "")
            except ValueError:
                continue
            if (url.scheme == "https" and url.hostname == "geo.captcha-delivery.com"
                    and url.path.rstrip("/") == "/captcha" and await frame.is_visible()):
                raise WalletError("CheckoutChallenge")

    async def require_checkout_access(self):
        await self.require_no_challenge()
        if await self.page.locator('[data-testid="checkout-generic-error-modal"]').is_visible():
            raise WalletError("CheckoutRejected")
        # Observed logged-out header. A stored JWT alone does not prove that the
        # browser is still signed in; report the page's actual state before Buy/Pay.
        for login in await self.page.locator('[data-testid="header--login-button"]').all():
            if await login.is_visible():
                raise WalletError("CheckoutLoginRequired")

    async def one(self, key, root=None):
        spec = self.contract["selectors"][key]
        elements = (root or self.page).locator(spec["css"])
        found = [e for e in await elements.all() if await e.is_visible()]
        if len(found) != 1:
            raise WalletError("ContractMismatch")
        return found[0]

    async def value(self, key, root):
        element = await self.one(key, root)
        attr = self.contract["selectors"][key].get("attribute")
        result = await element.get_attribute(attr) if attr else await element.text_content()
        if not result or not result.strip():
            raise WalletError("ContractMismatch")
        return result.strip()

    async def snapshot(self, expected=None):
        if expected is not None:
            autobuy.require_available()
        self.trusted()
        await self.require_checkout_access()
        if await address_requirement(self.page, self.origin):
            raise WalletError("CheckoutAddressRequired")
        if await phone_requirement(self.page, self.origin):
            raise WalletError("CheckoutPhoneRequired")
        return await self.page.evaluate(
            SNAPSHOT,
            {
                "selectors": self.contract["selectors"],
                "fields": sorted(FIELDS),
                "origin": self.origin,
                "expected": expected,
            },
        )

    def quote(self, raw, *, require_wallet=True):
        if raw["account_key"] != self.contract["expected_account"]:
            raise WalletError("ManualAction")
        is_wallet = raw["payment_method"] == self.contract["wallet_label"]
        if require_wallet and not is_wallet:
            raise WalletError("WalletOnly")
        delivery = self.contract.get("expected_delivery")
        if (self.contract.get("delivery_mode") != "vinted"
                and delivery and raw["delivery_key"] != delivery):
            raise WalletError("DeliveryChanged")
        currency = raw["currency"]
        if currency != self.account.currency:
            raise WalletError("CheckoutChanged")
        values = dict(raw)
        for key in MONEY:
            values[key] = money_text(raw[key], currency, self.contract["decimal_separator"])
        values["payment_method"] = "wallet" if is_wallet else "external"
        return Quote(**values)

    async def checkout(self, order):
        autobuy.require_available()
        from playwright.async_api import TimeoutError as BrowserTimeout

        region, item_id = listing_identity(order.url)
        if (
            region != self.account.region
            or item_id != order.item_id
            or self.contract["region"] != region
        ):
            raise WalletError("ContractMismatch")
        current = urlsplit(self.page.url)
        if (self.prepared_item == item_id
                and f"{current.scheme}://{current.netloc}" == self.origin
                and current.path.rstrip("/") == "/checkout"):
            # The same verified checkout is already open. Refresh it from the server;
            # do not reuse the old quote or revisit the listing and click Buy again.
            await self.page.reload(wait_until="domcontentloaded", timeout=25_000)
        else:
            await self.page.goto(order.url, wait_until="domcontentloaded", timeout=25_000)
            self.trusted()
            await self.require_checkout_access()
            if listing_identity(self.page.url) != (region, item_id):
                raise WalletError("ContractMismatch")
            if not await listing_buy_ready(self.page, self.contract["selectors"]["listing_buy"]["css"]):
                raise WalletError("ManualAction")
            buy = await self.one("listing_buy")
            # The manually verified selector must denote only the listing Buy button.
            if await buy.evaluate("e => e.tagName") != "BUTTON":
                raise WalletError("ContractMismatch")
            await buy.click(timeout=10_000)
            try:
                await self.page.wait_for_url(
                    lambda url: urlsplit(url).path.rstrip("/") == "/checkout",
                    timeout=15_000,
                )
            except BrowserTimeout:
                await self.require_checkout_access()
                raise
        self.trusted()
        self.raw = await self.snapshot()
        return self.quote(self.raw)

    async def pay(self, order, quote):
        autobuy.require_available()
        if self.submitted or self.raw is None:
            raise WalletError("ContractMismatch")
        quote.check(order, self.account)
        # Mark locally before any browser command, including a command timeout.
        self.submitted = True
        await self.snapshot(expected=self.raw)
        for _ in range(20):
            self.trusted()
            try:
                root = await self.one("success_root")
                item_id = await self.value("order_item_id", root)
                external_id = await self.value("order_id", root)
                if item_id == order.item_id and re.fullmatch(r"[A-Za-z0-9_-]{1,80}", external_id):
                    return external_id
            except WalletError:
                pass
            await asyncio.sleep(0.5)
        # No success banner or navigation alone proves that this item was purchased.
        raise WalletError("ManualAction")


@asynccontextmanager
async def browser_adapter(account, *, probe=False, interactive=False, background=False):
    autobuy.require_available()
    contract, digest = load_contract(account.user_id, account.region)
    if contract["region"] != account.region or (not probe and digest != account.contract_hash):
        raise WalletError("ContractMismatch")
    from playwright.async_api import async_playwright

    async with profile_lock(account.user_id, account.region), async_playwright() as driver:
        async with wallet_context(
            driver, account.user_id, account.region, contract,
            headless=not interactive,
            locale="en-GB",
            service_workers="block",
            background=background,
        ) as context, wallet_page(
            context, {} if background else contract, preserve_challenge=not background,
        ) as page:
            origin = "https://" + REGIONS[account.region][0]

            page.on("popup", close_popup)
            page.on("dialog", dismiss_dialog)
            guard = background_navigation_guard if background else navigation_guard
            async with guard(page, origin):
                yield BrowserCheckout(page, account, contract)


def contract_template(region):
    # Empty selectors deliberately cannot enable purchases; adapt only from observed DOM.
    return {
        "version": 1,
        "region": region,
        "expected_account": "",
        "wallet_label": "",
        "decimal_separator": ".",
        "selectors": {key: {"css": ""} for key in sorted(SELECTORS)},
    }
