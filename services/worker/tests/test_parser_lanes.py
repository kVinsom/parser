import asyncio

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_monitoring import Parser, lot
from vpars_bot.db import utcnow
from vpars_parser.models import Capabilities, ParseRequest

from vpars_worker.parser import ParseFailure, ParserClient


@pytest.mark.parametrize("confirmed,accepted", [(True, True), (False, False)])
async def test_empty_inventory_must_have_explicit_proof_across_http(confirmed, accepted):
    async def catalog(request):
        query = ParseRequest.model_validate(await request.json())
        result = await Parser(items=[]).parse(query)
        result.identities_complete = True
        result.empty_confirmed = confirmed
        return web.json_response(result.model_dump(mode="json"))

    app = web.Application()
    app.router.add_post("/v1/catalog/parse", catalog)
    async with TestServer(app) as server:
        client = ParserClient(str(server.make_url("")))
        try:
            from time import monotonic

            from vpars_parser.models import Region

            client.capabilities = Capabilities(
                source_id="vinted",
                version="test",
                aliases={},
                regions=[Region(code="FR", name="France", base_url="https://www.vinted.fr")],
                filters={},
            )
            client.expires = monotonic() + 60
            if accepted:
                result = await client.parse(ParseRequest(region_id="FR", page=2))
                assert result.empty_confirmed and not result.items
            else:
                with pytest.raises(ParseFailure):
                    await client.parse(ParseRequest(region_id="FR", page=2))
        finally:
            await client.close()


@pytest.mark.parametrize(
    "changes",
    [
        {"cards_seen": 1},
        {"cards_skipped": 1},
        {"identities_complete": False},
        {"completeness": "partial"},
        {"observed_item_ids": ["123"]},
        {"items": [lot()]},
    ],
)
async def test_empty_proof_cannot_hide_partial_or_nonempty_inventory(changes):
    from vpars_worker.matching import validate_result

    request = ParseRequest(region_id="FR", page=2)
    result = await Parser(items=[]).parse(request)
    result = result.model_copy(
        update={"empty_confirmed": True, "identities_complete": True, **changes}
    )
    with pytest.raises(ValueError):
        validate_result(request, result)


async def test_details_http_does_not_block_catalog_and_identical_requests_are_shared():
    started, release = asyncio.Event(), asyncio.Event()
    calls = []

    async def details(request):
        data = await request.json()
        calls.append(data)
        started.set()
        await release.wait()
        return web.json_response(
            {
                "source_id": "vinted",
                "region_id": "FR",
                "source_item_id": "10001",
                "photo_urls": [],
                "photos_complete": False,
                "description": "Good condition",
                "publication_evidence": "relative:Just now",
                "fetched_at": utcnow().isoformat(),
            }
        )

    async def catalog(request):
        parsed = await Parser().parse(ParseRequest.model_validate(await request.json()))
        return web.json_response(parsed.model_dump(mode="json"))

    app = web.Application()
    app.router.add_post("/v1/items/details", details)
    app.router.add_post("/v1/catalog/parse", catalog)
    async with TestServer(app) as server:
        client = ParserClient(str(server.make_url("")))
        client.capabilities = Capabilities(
            source_id="vinted",
            version="test",
            aliases={},
            regions=[{"code": "FR", "name": "France", "base_url": "https://www.vinted.fr"}],
            filters={},
        )
        client.expires = float("inf")
        tasks = [asyncio.create_task(client.details(lot())) for _ in range(2)]
        try:
            await asyncio.wait_for(started.wait(), 3)
            result = await asyncio.wait_for(client.parse(ParseRequest(region_id="FR")), 3)
            assert result.items and all(not task.done() for task in tasks)
            release.set()
            await asyncio.gather(*tasks)
            cached = await client.details(lot())
            assert cached.description == "Good condition" and not cached.photos_complete
            assert len(calls) == 1
        finally:
            release.set()
            await asyncio.gather(*tasks, return_exceptions=True)
            await client.close()


async def test_region_cooldown_is_shared_between_catalog_and_details(monkeypatch):
    calls = []
    clock = [1000.0]
    monkeypatch.setattr("vpars_worker.parser.monotonic", lambda: clock[0])

    async def limited(request):
        calls.append(request.path)
        return web.json_response(
            {"error": {"code": "SourceRateLimited", "retry_after": 900}}, status=503
        )

    app = web.Application()
    app.router.add_post("/v1/items/details", limited)
    async with TestServer(app) as server:
        client = ParserClient(str(server.make_url("")))
        try:
            with pytest.raises(ParseFailure) as error:
                await client.details(lot())
            assert error.value.retry_after == 900
            clock[0] += 1.25
            with pytest.raises(ParseFailure) as error:
                await client.parse(ParseRequest(region_id="FR"))
            assert error.value.retry_after == 899
            assert calls == ["/v1/items/details"]
            client.check_cooldown("DE")
        finally:
            await client.close()


async def test_unknown_details_are_not_cached_and_retry_forces_public_html():
    requests = []

    async def details(request):
        data = await request.json()
        requests.append(data)
        return web.json_response(
            {
                "source_id": "vinted",
                "region_id": "FR",
                "source_item_id": "10001",
                "photo_urls": [],
                "photos_complete": False,
                "publication_evidence": "relative:just now" if data["force_html"] else "unknown",
                "fetched_at": utcnow().isoformat(),
            }
        )

    app = web.Application()
    app.router.add_post("/v1/items/details", details)
    async with TestServer(app) as server:
        client = ParserClient(str(server.make_url("")))
        try:
            assert (await client.details(lot())).publication_evidence == "unknown"
            assert (await client.details(lot())).publication_evidence == "unknown"
            assert (await client.refresh_details(lot())).publication_evidence == "relative:just now"
            assert (await client.details(lot())).publication_evidence == "relative:just now"
            assert [r["force_html"] for r in requests] == [False, False, True]
        finally:
            await client.close()


@pytest.mark.parametrize("region", ["FR", "GB"])
@pytest.mark.parametrize("code", ["SourceRateLimited", "AccessBlocked"])
async def test_local_queue_checks_do_not_extend_source_cooldown(monkeypatch, region, code):
    clock = [1000.0]
    monkeypatch.setattr("vpars_worker.parser.monotonic", lambda: clock[0])
    calls = []

    async def limited(request):
        calls.append(request.path)
        return web.json_response({"error": {"code": code, "retry_after": 120}}, status=503)

    app = web.Application()
    app.router.add_post("/v1/items/details", limited)
    async with TestServer(app) as server:
        client = ParserClient(str(server.make_url("")))
        item = lot().model_copy(update={"region_id": region})
        try:
            with pytest.raises(ParseFailure):
                await client.details(item)
            for _ in range(20):
                clock[0] += 0.1
                with pytest.raises(ParseFailure):
                    await client.details(item)
            assert calls == ["/v1/items/details"]
            clock[0] = 1120.0
            client.check_cooldown(region)
            with pytest.raises(ParseFailure):
                await client.details(item)
            assert len(calls) == 2
        finally:
            await client.close()


@pytest.mark.parametrize("region", ["FR", "GB"])
@pytest.mark.parametrize("code", ["SourceRateLimited", "AccessBlocked"])
async def test_concurrent_shorter_refusal_cannot_shorten_retry_after(monkeypatch, region, code):
    clock = [1000.0]
    monkeypatch.setattr("vpars_worker.parser.monotonic", lambda: clock[0])
    client = ParserClient()
    try:
        client.remember_failure(region, ParseFailure(code, retry_after=900))
        clock[0] += 1
        client.remember_failure(region, ParseFailure(code, retry_after=120))
        clock[0] = 1500
        with pytest.raises(ParseFailure) as error:
            client.check_cooldown("UK" if region == "GB" else region)
        assert error.value.retry_after == 400
    finally:
        await client.close()
