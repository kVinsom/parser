"""Local-only login/probe; no tokens passed through Telegram or command arguments."""

import argparse
import asyncio
import json
import sys
from types import SimpleNamespace

from . import autobuy_availability as autobuy
from .application import locked_user
from .config import Settings
from .db import WalletAccount, database
from .wallet import REGIONS, WalletError, listing_identity, minor
from .wallet_accounts import wallet_account
from .wallet_browser import (
    browser_adapter,
    browser_options,
    cdp_endpoint,
    contract_path,
    contract_template,
    load_contract,
    profile_lock,
    wallet_context,
)


async def main_async(args):
    if not autobuy.AVAILABLE:
        print(autobuy.SOON)
        return
    selected_cdp = getattr(args, "cdp_url", None)
    if selected_cdp is not None:
        selected_cdp = cdp_endpoint({"browser_cdp_url": selected_cdp})
    engine, sessions = database(Settings().database_url.get_secret_value())
    try:
        async with sessions.begin() as session:
            await locked_user(session, args.user_id)
            account = await wallet_account(session, args.user_id, getattr(args, "region", None))
            if account and account.active_order and args.command != "disable":
                raise WalletError("FinishOrReconcilePreviousOrder")
            if args.command in {"connect", "login"}:
                if args.command == "connect":
                    limit = minor(args.limit)
                    if limit <= 0:
                        raise WalletError("PositiveLimitRequired")
                else:
                    # Login/DOM inspection does not authorize a purchase budget.
                    # Preserve an existing budget; a new account cannot spend anything.
                    if account is not None and account.region != args.region:
                        raise WalletError("WrongRegion")
                    limit = account.max_total if account is not None else 0
                if account is None:
                    account = WalletAccount(user_id=args.user_id, region=args.region)
                    session.add(account)
                account.region, account.currency = args.region, REGIONS[args.region][1]
                account.max_total, account.enabled, account.contract_hash = limit, False, None
            elif account is None:
                raise WalletError("ConnectFirst")
            else:
                account.enabled = False
        if args.command == "disable":
            print("Autobuy вимкнено. Пошук не змінено.")
            return
        if args.command in {"connect", "login"}:
            from playwright.async_api import async_playwright

            path = contract_path(args.user_id, account.region)
            if path.exists():
                config = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(config, dict):
                    raise WalletError("ContractMismatch")
            else:
                config = contract_template(args.region)
            region_changed = config.get("region") != args.region
            if region_changed:
                # A country change confirmed in the Telegram wizard needs a fresh
                # checkout contract; account/delivery bindings belong to the old region.
                options = browser_options(config)
                config = contract_template(args.region)
                if options.get("channel"):
                    config["browser_channel"] = options["channel"]
            selected_browser = getattr(args, "browser", None)
            if selected_browser is not None:
                config["browser_channel"] = selected_browser
                config.pop("browser_cdp_url", None)
            if selected_cdp is not None:
                config["browser_cdp_url"] = selected_cdp
            browser_options(config)
            if (not path.exists() or selected_browser is not None
                    or selected_cdp is not None or region_changed):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    json.dumps(config, indent=2), encoding="utf-8"
                )
            print("Увійди у Vinted у відкритому браузері. Оплата автобаю вимкнена.", flush=True)
            async with profile_lock(args.user_id, account.region), async_playwright() as driver:
                async with wallet_context(
                    driver, args.user_id, account.region, config,
                    headless=False,
                    locale="en-GB",
                ) as context:
                    page = (
                        context.pages[0] if context.pages and not cdp_endpoint(config)
                        else await context.new_page()
                    )
                    await page.goto(
                        "https://" + REGIONS[args.region][0],
                        wait_until="domcontentloaded", timeout=30_000,
                    )
                    print("Браузер Vinted відкрито. Очікую твій ручний вхід.", flush=True)
                    await asyncio.to_thread(input, "Коли завершиш вхід, натисни Enter тут: ")
            print(
                "Профіль збережено локально. Перед увімкненням потрібна перевірка checkout-контракту."
            )
            return
        contract, digest = load_contract(args.user_id, account.region)
        region, item_id = listing_identity(args.url)
        if region != account.region:
            raise WalletError("WrongRegion")
        # Probe opens a checkout and reads its quote, but never calls adapter.pay().
        order = SimpleNamespace(
            url=args.url,
            item_id=item_id,
            currency=account.currency,
            listed_price=minor(args.price),
        )
        async with browser_adapter(account, probe=True, interactive=True) as adapter:
            if args.command == "delivery":
                # Existing delivery binding must not prevent deliberately choosing a new one.
                adapter.contract = {k: v for k, v in adapter.contract.items()
                                    if k != "expected_delivery"}
            quote = await adapter.checkout(order)
            if args.command == "delivery":
                print(
                    "У відкритому оформленні вибери доставку: збережену адресу або пункт видачі. "
                    "Не натискай оплату."
                )
                await asyncio.to_thread(input, "Коли вибір готовий, натисни Enter тут: ")
                quote = adapter.quote(await adapter.snapshot())
            quote.check(order, account)
        if args.command == "delivery":
            # Keep other selector/account settings exactly as inspected. The hash changes,
            # so purchases stay disabled until the normal verified --enable step succeeds.
            contract["expected_delivery"] = quote.delivery_key
            path = contract_path(args.user_id, account.region)
            pending = path.with_suffix(".json.tmp")
            pending.write_text(json.dumps(contract, indent=2, ensure_ascii=False), encoding="utf-8")
            pending.replace(path)
            _, digest = load_contract(args.user_id, account.region)
            print("Вибір доставки збережено. Інша адреса або пункт блокуватимуть оплату.")
        print(f"Checkout перевірено: {quote.total / 100:.2f} {quote.currency}; тільки баланс.")
        if args.enable:
            async with sessions.begin() as session:
                await locked_user(session, args.user_id)
                current = await wallet_account(session, args.user_id, account.region)
                if current.active_order or current.region != account.region:
                    raise WalletError("ConfigurationChanged")
                quote.check(order, current)
                if load_contract(args.user_id, account.region)[1] != digest:
                    raise WalletError("ContractMismatch")
                current.contract_hash, current.enabled = digest, True
            print("Buy увімкнено. Кожна покупка потребує підтвердження повної суми в Telegram.")
        else:
            print("Оплата вимкнена. Перевірено тільки читання; гроші не списувалися.")
    finally:
        await engine.dispose()


def main():
    parser = argparse.ArgumentParser(
        description="Local Vinted wallet setup (no secrets in arguments)"
    )
    parser.add_argument("command", choices=["login", "connect", "probe", "delivery", "disable"])
    parser.add_argument("--user-id", required=True, type=int)
    parser.add_argument("--region", choices=REGIONS)
    connection = parser.add_mutually_exclusive_group()
    connection.add_argument("--browser", choices=["chromium", "chrome", "msedge"],
                        help="Browser for this dedicated profile, saved for future probes/purchases")
    connection.add_argument("--cdp-url", help="Existing dedicated browser: http://127.0.0.1:PORT")
    parser.add_argument("--limit", help="Total per purchase in account currency, including fees")
    parser.add_argument("--url", help="A Vinted listing to inspect without payment")
    parser.add_argument("--price", help="Expected listing price, excluding fees")
    parser.add_argument(
        "--enable", action="store_true", help="Enable Buy only after a successful probe"
    )
    args = parser.parse_args()
    if args.user_id <= 0:
        parser.error("user-id must be positive")
    if args.command == "connect" and (not args.region or not args.limit):
        parser.error("connect requires --region and --limit")
    if args.command == "login" and not args.region:
        parser.error("login requires --region")
    if args.command == "login" and (args.enable or args.limit is not None):
        parser.error("login does not accept a purchase budget or enable payments")
    if (args.browser is not None or args.cdp_url is not None) and args.command not in {"login", "connect"}:
        parser.error("choose --browser/--cdp-url during login/connect; probes reuse the saved browser")
    if args.command in {"probe", "delivery"} and (not args.url or args.price is None):
        parser.error("probe/delivery requires --url and --price")
    sys.stdout.reconfigure(encoding="utf-8")
    try:
        asyncio.run(main_async(args))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        code = str(exc) if isinstance(exc, WalletError) else type(exc).__name__
        parser.exit(1, f"Підключення не завершено ({code}). Оплата не вмикалася.\n")


if __name__ == "__main__":
    main()
