"""Run in the isolated Linux test stack, never against the user's database."""

import asyncio
import os
import signal
import sys


async def browser():
    from playwright.async_api import async_playwright

    async with async_playwright() as driver:
        client = await driver.chromium.launch(headless=True, chromium_sandbox=True)
        page = await client.new_page()
        await page.set_content("<title>Server browser OK</title>")
        assert await page.title() == "Server browser OK"
        await client.close()
    print("Chromium sandbox: OK")


async def bot():
    from aiogram.client.telegram import TelegramAPIServer
    from aiohttp import web
    from sqlalchemy import text
    from vpars_bot import runtime
    from vpars_bot.config import Settings
    from vpars_bot.db import database

    sys.path.insert(0, "/app/deploy")
    from service import health, serve

    config = Settings()
    assert (
        config.telegram_bot_token.get_secret_value() == "999999:server-smoke-test-token"
    )
    engine, _ = database(config.database_url.get_secret_value())
    async with engine.connect() as connection:
        assert await connection.scalar(text("SELECT count(*) FROM bot_users")) == 0
    await engine.dispose()
    methods = []

    async def telegram(request):
        method = request.match_info["method"]
        methods.append(method)
        if method == "getUpdates":
            await asyncio.sleep(0.2)
            result = []
        elif method == "getMe":
            result = {
                "id": 999999,
                "is_bot": True,
                "first_name": "Isolated test",
                "username": "test_bot",
            }
        else:
            result = True
        return web.json_response({"ok": True, "result": result})

    app = web.Application()
    app.router.add_post("/bot{token}/{method}", telegram)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 18089)
    await site.start()
    original = runtime.Bot

    def isolated_bot(*args, **kwargs):
        kwargs["session"].api = TelegramAPIServer.from_base("http://127.0.0.1:18089")
        return original(*args, **kwargs)

    runtime.Bot = isolated_bot
    serving = asyncio.create_task(serve(lambda: runtime.run(config)))
    try:
        for _ in range(80):
            if serving.done():
                await serving
                raise AssertionError("Bot stopped before readiness")
            try:
                await health("bot")
                if "getUpdates" in methods:
                    break
            except RuntimeError:
                pass
            await asyncio.sleep(0.1)
        else:
            raise AssertionError("Bot failed readiness")
        os.kill(os.getpid(), signal.SIGTERM)
        await asyncio.wait_for(serving, timeout=15)
        try:
            await health("bot")
        except RuntimeError:
            pass
        else:
            raise AssertionError("Bot lock leaked after SIGTERM")
        assert "getUpdates" in methods
    finally:
        if not serving.done():
            serving.cancel()
            await asyncio.gather(serving, return_exceptions=True)
        runtime.Bot = original
        await runner.cleanup()
    print("Bot polling against local stub, readiness and graceful SIGTERM: OK")


async def main():
    await browser()
    await bot()


if __name__ == "__main__":
    asyncio.run(main())
