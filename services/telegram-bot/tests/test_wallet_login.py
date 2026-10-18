
"""Manual login binds a profile without inventing spending authorization."""

import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from vpars_bot.application import Catalog
from vpars_bot.db import WalletAccount
from vpars_bot.wallet import WalletError
from vpars_bot.wallet_accounts import wallet_account


@pytest.mark.parametrize("existing_limit", [None, 4200])
@pytest.mark.parametrize("browser_channel", [None, "chrome"])
@pytest.mark.parametrize("old_region", [None, "UK"])
async def test_login_disables_payment_and_preserves_existing_budget(
    db,
    registry,
    monkeypatch,
    tmp_path,
    existing_limit,
    browser_channel,
    old_region,
):
    import playwright.async_api

    import vpars_bot.wallet_browser as browser_module
    import vpars_bot.wallet_setup as setup_module

    catalog = Catalog(db[1], registry)
    await catalog.register(101, 101, "login_test", "Test", 1, start=True)
    before = await catalog.user(101)
    if existing_limit is not None:
        async with db[1].begin() as session:
            session.add(
                WalletAccount(
                    user_id=101,
                    region="DE",
                    currency="EUR",
                    max_total=existing_limit,
                    enabled=True,
                    contract_hash="a" * 64,
                )
            )

    class Context:
        def __init__(self):
            self.pages = [self]
            self.urls = []
            self.closed = False

        async def goto(self, url, **kwargs):
            self.urls.append(url)

        async def close(self):
            self.closed = True

        async def launch_persistent_context(self, path, **kwargs):
            assert path == str(browser_module.profile_dir(101, "DE"))
            assert kwargs["headless"] is False
            assert kwargs.get("channel") == browser_channel
            return self

    context = Context()

    @asynccontextmanager
    async def browser():
        yield SimpleNamespace(chromium=context)

    monkeypatch.setattr(browser_module, "ROOT", tmp_path)
    monkeypatch.setattr(playwright.async_api, "async_playwright", browser)
    monkeypatch.setattr(
        setup_module, "Settings", lambda: SimpleNamespace(database_url=SecretStr("unused"))
    )
    monkeypatch.setattr(setup_module, "database", lambda _: db)
    monkeypatch.setattr("builtins.input", lambda _: "")
    if old_region:
        old_contract = browser_module.contract_template(old_region)
        old_contract["expected_account"] = "old-account"
        old_contract["expected_delivery"] = "old-address"
        if browser_channel:
            old_contract["browser_channel"] = browser_channel
        path = browser_module.contract_path(101, "DE")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(old_contract), encoding="utf-8")
    await setup_module.main_async(
        SimpleNamespace(command="login", user_id=101, region="DE", browser=browser_channel)
    )
    async with db[1]() as session:
        account = await wallet_account(session, 101)
        assert (account.region, account.currency) == ("DE", "EUR")
        assert account.max_total == (existing_limit or 0)
        assert not account.enabled and account.contract_hash is None
    after = await catalog.user(101)
    assert before.delivery_epoch == after.delivery_epoch
    assert before.enabled_sources == after.enabled_sources
    assert context.urls == ["https://www.vinted.de"] and context.closed
    config = json.loads(browser_module.contract_path(101, "DE").read_text(encoding="utf-8"))
    assert config["region"] == "DE" and config["expected_account"] == ""
    assert "expected_delivery" not in config
    # Login never substitutes fake selectors for an inspected real checkout contract.
    with pytest.raises(WalletError, match="ContractMismatch"):
        browser_module.load_contract(101, "DE")


@pytest.mark.parametrize("channel", ["unknown", "http://external.invalid", []])
def test_browser_selection_accepts_only_named_local_browsers(channel):
    from vpars_bot.wallet_browser import browser_options

    with pytest.raises(WalletError, match="ContractMismatch"):
        browser_options({"browser_channel": channel})
