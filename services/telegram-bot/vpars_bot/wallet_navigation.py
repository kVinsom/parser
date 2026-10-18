"""Restrict document navigation without disabling Chromium's HTTP cache."""

import asyncio
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlsplit

from .wallet import WalletError


def same_origin(url, origin):
    value = urlsplit(url)
    return f"{value.scheme}://{value.netloc}" == origin


@asynccontextmanager
async def background_navigation_guard(page, origin):
    """Stop background checkout on a foreign main document without request interception.

    Fetch interception broke the live checkout XHR even with Document-only filters.
    This monitor closes a foreign page after navigation; it does not prevent its first
    network request. Background checkout has no remote pointer/text input or streaming.
    BrowserCheckout validates the origin before reads/actions and again in its atomic
    payment snapshot. Interactive handoff retains the pre-request guard below.
    """
    blocked = False
    tasks = set()

    async def close():
        with suppress(Exception):
            await page.close()

    def navigated(frame):
        nonlocal blocked
        if frame != page.main_frame or same_origin(frame.url, origin):
            return
        blocked = True
        task = asyncio.create_task(close())
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    page.on("framenavigated", navigated)
    try:
        if page.url != "about:blank":
            navigated(page.main_frame)
        if blocked:
            raise WalletError("ManualAction")
        yield
    finally:
        page.remove_listener("framenavigated", navigated)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if blocked:
            raise WalletError("ManualAction")


@asynccontextmanager
async def navigation_guard(page, origin):
    # Playwright page.route disables HTTP caching even for nonmatching resources.
    # Server browsers are Chromium; intercept only document requests through CDP.
    # Scripts, cookies, fingerprints and challenge responses are not modified.
    client = await page.context.new_cdp_session(page)
    tasks = set()
    try:
        await client.send("Page.enable")
        tree = await client.send("Page.getFrameTree")
        main_frame = tree["frameTree"]["frame"]["id"]

        def navigated(event):
            nonlocal main_frame
            frame = event.get("frame", {})
            if not frame.get("parentId") and frame.get("id"):
                main_frame = frame["id"]

        async def handle(event):
            try:
                frame = event.get("frameId")
                foreign = not same_origin(event["request"]["url"], origin)
                if foreign and (not frame or frame == main_frame):
                    await client.send("Fetch.failRequest", {
                        "requestId": event["requestId"], "errorReason": "BlockedByClient",
                    })
                else:
                    # Foreign child frames (including a user's CAPTCHA) load normally.
                    await client.send("Fetch.continueRequest", {"requestId": event["requestId"]})
            except Exception:
                # A failed navigation guard must never expose an unguarded remote page.
                with suppress(Exception):
                    await page.close()

        def paused(event):
            task = asyncio.create_task(handle(event))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        client.on("Page.frameNavigated", navigated)
        client.on("Fetch.requestPaused", paused)
        await client.send("Fetch.enable", {"patterns": [{
            "resourceType": "Document", "requestStage": "Request",
        }]})
        yield
    finally:
        with suppress(Exception):
            await client.send("Fetch.disable")
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        with suppress(Exception):
            await client.detach()
