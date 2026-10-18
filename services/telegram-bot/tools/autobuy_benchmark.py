"""Local checkout timing experiment; never uses real profiles, Telegram or bot DB.

Compares launching Chromium after confirmation with an already running Chromium.
Both paths reopen and recheck checkout. This does not implement a production pool.
"""

import argparse
import asyncio
import json
from contextlib import asynccontextmanager
from pathlib import Path
from statistics import median
from time import perf_counter
from types import SimpleNamespace

from playwright.async_api import async_playwright

from tools.autobuy_mock import MockBrowser, MockCheckout, serve
from vpars_bot.wallet import REGIONS


class TimedMock(MockCheckout):
    pay_received_at = None

    async def pay(self, request):
        self.pay_received_at = perf_counter()
        return await super().pay(request)


@asynccontextmanager
async def chromium():
    async with async_playwright() as driver:
        browser = await driver.chromium.launch(headless=True, chromium_sandbox=True)
        try:
            yield browser
        finally:
            await browser.close()


async def measure(mode, site, endpoint, user_id, region, item_id, browser=None):
    account = SimpleNamespace(
        user_id=user_id, region=region, currency=REGIONS[region][1], max_total=2000
    )
    order = SimpleNamespace(
        item_id=item_id,
        url=f"https://{REGIONS[region][0]}/items/{item_id}",
        currency=account.currency,
        listed_price=site.items[item_id][1],
    )

    @asynccontextmanager
    async def adapter():
        if browser is not None:
            async with MockBrowser(browser, endpoint).adapter(account) as checkout:
                yield checkout
        else:
            async with chromium() as cold_browser:
                async with MockBrowser(cold_browser, endpoint).adapter(account) as checkout:
                    yield checkout

    # First click: prepare the quote, then close the checkout as the current worker does.
    started = perf_counter()
    async with adapter() as checkout:
        quote = await checkout.checkout(order)
        quote.check(order, account)
        quote_ready = perf_counter() - started

    # Second click: measure until the HTTP service actually receives Pay, separately
    # from waiting for and detecting the successful result.
    site.pay_received_at = None
    started = perf_counter()
    async with adapter() as checkout:
        adapter_ready = perf_counter() - started
        current = await checkout.checkout(order)
        current.check(order, account)
        assert current.same_purchase(quote)
        rechecked = perf_counter() - started
        external_id = await checkout.pay(order, current)
        result_ready = perf_counter() - started
    assert external_id == f"mock-{user_id}-{item_id}"
    assert site.accounts[str(user_id)].debits == 1
    return {
        "mode": mode,
        "region": region,
        "brand": "Amiri" if item_id == "123" else "Stone Island",
        "first_click_to_quote_ms": round(quote_ready * 1000),
        "confirmation_to_browser_ready_ms": round(adapter_ready * 1000),
        "confirmation_to_rechecked_ms": round(rechecked * 1000),
        "confirmation_to_pay_received_ms": round((site.pay_received_at - started) * 1000),
        "confirmation_to_result_ms": round(result_ready * 1000),
    }


async def main(output):
    rows = []
    for mode in ("fresh_browser", "already_running_browser"):
        site = TimedMock()
        async with serve(site) as endpoint:
            if mode == "fresh_browser":
                for i, (key, account) in enumerate(site.accounts.items()):
                    rows.append(
                        await measure(
                            mode,
                            site,
                            endpoint,
                            int(key),
                            account.region,
                            "123" if i % 2 == 0 else "456",
                        )
                    )
            else:
                async with chromium() as browser:
                    for i, (key, account) in enumerate(site.accounts.items()):
                        rows.append(
                            await measure(
                                mode,
                                site,
                                endpoint,
                                int(key),
                                account.region,
                                "123" if i % 2 == 0 else "456",
                                browser,
                            )
                        )
    summaries = {}
    for mode in {row["mode"] for row in rows}:
        samples = [row for row in rows if row["mode"] == mode]
        summaries[mode] = {
            key: {
                "median": median([row[key] for row in samples]),
                "min": min(row[key] for row in samples),
                "max": max(row[key] for row in samples),
            }
            for key in samples[0]
            if key.endswith("_ms")
        }
    report = {
        "scope": "Local HTTP mock only. No Telegram, database queue, WAN or actual Vinted timing.",
        "samples_per_mode": 4,
        "summary": summaries,
        "samples": rows,
    }
    encoded = json.dumps(report, indent=2)
    if output:
        Path(output).write_text(encoded, encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output")
    asyncio.run(main(parser.parse_args().output))
