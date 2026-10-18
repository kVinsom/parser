from decimal import Decimal

import pytest

from vpars_bot.config import Settings
from vpars_bot.domain import Filters, Price, Source, SourceRegistry


def test_no_token_needed_for_settings_or_tests():
    settings = Settings(_env_file=None, telegram_bot_token="")
    assert not settings.telegram_bot_token.get_secret_value()
    assert settings.update_mode == "polling" and settings.search_adapter == "unavailable"
    assert settings.menu_mode == "menu"


@pytest.mark.parametrize(
    "fields",
    [
        {"update_mode": "both"},
        {"menu_mode": "unknown"},
        {"app_env": "production", "search_adapter": "demo"},
        {"update_mode": "webhook", "webhook_base_url": "http://example.com"},
        {"update_mode": "webhook", "webhook_base_url": "https://example.com", "webhook_secret": ""},
        {"database_url": "sqlite:///file.db"},
    ],
)
def test_invalid_runtime_configuration(fields):
    with pytest.raises(ValueError):
        Settings(_env_file=None, **fields)


def test_poland_cannot_be_reenabled():
    with pytest.raises(ValueError, match="disabled"):
        SourceRegistry({"vinted": Source(label="Vinted", regions=["PL"])})
    with pytest.raises(ValueError, match="disabled"):
        SourceRegistry({"vinted_pl": Source(label="Vinted PL", provider="vinted", regions=["PL"])})


def test_vinted_countries_are_independent_configured_marketplaces(registry):
    assert set(registry.sources) == {"vinted_gb", "vinted_fr", "vinted_it", "vinted_de"}
    for source_id, source in registry.sources.items():
        assert source.provider == "vinted"
        assert source.regions == [source_id[-2:].upper()]
        assert registry.region_label(source_id, source.regions[0]) == source.label


async def test_capability_contract_never_invents_dictionaries(registry):
    report = await registry.describe("vinted_fr", ["FR"])
    assert not report.verified
    assert report.filters["colors"].status == "unsupported"
    assert report.filters["models"].status == "unknown"
    assert all(capability.values is None for capability in report.filters.values())


async def test_euro_price_is_converted_for_other_markets(registry):
    f = Filters(
        sources={"vinted": ["UK", "FR"]}, price=Price(currency="EUR", maximum=Decimal("80"))
    )
    assert not await registry.issues(f)
    converted = registry.for_source(registry.validate(f), "vinted_gb")
    assert converted.price.currency == "GBP" and converted.price.maximum == Decimal("68")
    assert f.price.currency == "EUR"
    assert not await registry.issues(Filters(sources={"vinted": ["GB", "FR"]}))
