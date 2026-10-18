"""ECB reference rates shared by menu, searches and delivery (no per-user rates)."""

import asyncio
import json
import logging
import ssl
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_UP, Decimal
from xml.etree import ElementTree

import certifi
from aiohttp import ClientSession, ClientTimeout

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"
CACHE_KEY = "vpars:exchange:ecb:v1"
MAX_AGE_DAYS = 7  # Includes weekends and bank holidays; never use indefinite stale rates.
logger = logging.getLogger("vpars_bot.exchange")


class ExchangeRateUnavailable(ValueError):
    pass


class ExchangeRates:
    def __init__(self):
        self.as_of = None
        self.rates = {}
        self.refresh_seconds = 60

    def load(self, data):
        as_of = date.fromisoformat(data["date"])
        rates = {key: Decimal(value) for key, value in data["rates"].items()}
        if not rates or any(not r.is_finite() or r <= 0 for r in rates.values()):
            raise ValueError("Invalid exchange rates")
        if not 0 <= (datetime.now(UTC).date() - as_of).days <= MAX_AGE_DAYS:
            raise ValueError("Stale or future exchange rates")
        if rates.get("EUR", Decimal(1)) != 1:
            raise ValueError("Expected EUR base")
        if self.as_of is None or as_of >= self.as_of:
            self.as_of, self.rates = as_of, {**rates, "EUR": Decimal(1)}

    def convert(self, amount, currency):
        if amount is None or currency == "EUR":
            return amount
        if (
            self.as_of is None
            or not 0 <= (datetime.now(UTC).date() - self.as_of).days <= MAX_AGE_DAYS
            or currency not in self.rates
        ):
            raise ExchangeRateUnavailable(currency)
        return (amount * self.rates[currency]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    async def refresh(self, redis):
        # Both services retain the last verified snapshot across restarts.
        try:
            cached = await redis.get(CACHE_KEY)
            if cached:
                self.load(json.loads(cached))
        except (ValueError, KeyError, TypeError):
            logger.warning("Ignoring invalid or stale exchange-rate cache")
        try:
            async with ClientSession(timeout=ClientTimeout(total=8), trust_env=True) as http:
                async with http.get(
                    ECB_URL, ssl=ssl.create_default_context(cafile=certifi.where())
                ) as response:
                    response.raise_for_status()
                    data = parse_ecb(await response.text())
            self.load(data)
            await redis.set(CACHE_KEY, json.dumps(data), ex=MAX_AGE_DAYS * 86400)
            logger.info("ECB rates loaded date=%s GBP=%s", self.as_of, self.rates.get("GBP"))
            self.refresh_seconds = 3600
            return True
        except Exception as exc:
            # No guessed rate and no unbounded searches on a provider outage.
            logger.warning(
                "Exchange-rate refresh failed (%s); cached_date=%s", type(exc).__name__, self.as_of
            )
            self.refresh_seconds = 60
            return False

    async def run(self, redis):
        while True:
            await asyncio.sleep(self.refresh_seconds)
            await self.refresh(redis)


def parse_ecb(document):
    root = ElementTree.fromstring(document)
    day = next(node for node in root.iter() if "time" in node.attrib)
    rates = {
        node.attrib["currency"]: node.attrib["rate"]
        for node in day
        if "currency" in node.attrib and "rate" in node.attrib
    }
    if "GBP" not in rates:
        raise ValueError("ECB response has no GBP rate")
    return {"date": day.attrib["time"], "rates": rates}
