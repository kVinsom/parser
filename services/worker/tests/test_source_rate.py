import asyncio
import json
from datetime import timedelta
from time import monotonic

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer
from test_monitoring import lot
from vpars_bot.db import utcnow
from vpars_parser.models import ParseRequest

from vpars_worker.parser import ParseFailure, ParserClient
from vpars_worker.source_rate import SourcePacer


class Clock:
    now = 1000.0

    def __call__(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds
        await asyncio.sleep(0)


@pytest.mark.parametrize("regions", [("FR", "DE"), ("UK", "IT")])
async def test_catalog_and_detail_requests_share_global_start_budget(regions):
    clock = Clock()
    pacer = SourcePacer(clock=clock, sleep=clock.sleep)
    starts = []
    for region in [*regions, *regions]:
        await pacer.acquire(region, lambda _: None)
        starts.append(clock())
    assert starts == [1000, 1001, 1002, 1003]


async def test_limited_region_slows_down_without_delaying_others():
    clock = Clock()
    pacer = SourcePacer(clock=clock, sleep=clock.sleep)
    pacer.limited("FR")
    await pacer.acquire("FR", lambda _: None)
    await pacer.acquire("DE", lambda _: None)
    assert clock() == 1001
    await pacer.acquire("FR", lambda _: None)
    assert clock() == 1004
    for _ in range(5):
        pacer.limited("FR")
    assert pacer.regions["FR"] == 16
    for _ in range(10):
        pacer.succeeded("FR")
    assert pacer.regions["FR"] == 16
    clock.now += 121
    pacer.succeeded("FR")
    assert pacer.regions["FR"] == 16
    clock.now += 480
    for _ in range(30):
        pacer.succeeded("FR")
    assert pacer.regions["FR"] == 8
    for _ in range(30):
        pacer.succeeded("FR")
    assert pacer.regions["FR"] == 8  # Another stable window precedes another speed-up.


@pytest.mark.parametrize("region", ["FR", "UK"])
@pytest.mark.parametrize("active_pause", [False, True])
async def test_learned_interval_survives_restart_even_after_pause_expiry(
    tmp_path, region, active_pause
):
    path = tmp_path / "cooldowns.json"
    first = ParserClient(state_path=path)
    try:
        for _ in range(3):
            first.pacer.limited(region)
        assert first.pacer.regions[region] == 16
        if active_pause:
            from time import monotonic

            first.cooldowns[region] = ("SourceRateLimited", monotonic() + 900)
    finally:
        await first.close()
    second = ParserClient(state_path=path)
    try:
        assert second.pacer.regions[region] == 16
        for _ in range(50):
            second.pacer.succeeded(region)
        assert second.pacer.regions[region] == 16
        if active_pause:
            with pytest.raises(ParseFailure) as error:
                second.check_cooldown(region)
            assert 898 <= error.value.retry_after <= 900
        else:
            second.check_cooldown(region)
        assert "DE" not in second.pacer.regions
        second.check_cooldown("DE")
    finally:
        await second.close()


async def test_refusal_while_waiting_prevents_another_request():
    clock = Clock()
    pacer = SourcePacer(clock=clock, sleep=clock.sleep)
    checks = 0

    def cooldown(_):
        nonlocal checks
        checks += 1
        if checks == 3:
            raise RuntimeError("new source cooldown")

    await pacer.acquire("FR", cooldown)
    with pytest.raises(RuntimeError, match="new source cooldown"):
        await pacer.acquire("FR", cooldown)
    assert pacer.next_start == 1001


@pytest.mark.parametrize("region", ["FR", "UK"])
async def test_verified_html_refresh_is_shared_until_its_original_age_proof_expires(region):
    calls = []

    async def details(request):
        calls.append(await request.json())
        return web.json_response(
            {
                "source_id": "vinted",
                "region_id": region,
                "source_item_id": "10001",
                "photo_urls": [],
                "photos_complete": False,
                "publication_evidence": "relative:just now",
                "fetched_at": utcnow().isoformat(),
            }
        )

    app = web.Application()
    app.router.add_post("/v1/items/details", details)
    async with TestServer(app) as server:
        client = ParserClient(str(server.make_url("")))
        item = lot().model_copy(update={"region_id": region})
        try:
            first = await client.refresh_details(item)
            second = await client.refresh_details(item)
            assert second.fetched_at == first.fetched_at
            assert len(calls) == 1 and calls[0]["force_html"]
            key = ("vinted", region, "10001", True)
            expires, cached = client.detail_cache[key]
            client.detail_cache[key] = (
                0,
                cached.model_copy(update={"fetched_at": utcnow() - timedelta(seconds=6)}),
            )
            await client.refresh_details(item)
            assert len(calls) == 2
        finally:
            await client.close()


@pytest.mark.parametrize("region", ["FR", "UK"])
async def test_catalog_and_details_use_the_same_pacer_and_api_cannot_satisfy_html(
    region, monkeypatch
):
    clock, calls = Clock(), []

    async def details(request):
        data = await request.json()
        calls.append(("html" if data["force_html"] else "api", clock()))
        return web.json_response(
            {
                "source_id": "vinted",
                "region_id": region,
                "source_item_id": "10001",
                "photo_urls": [],
                "photos_complete": False,
                "publication_evidence": "relative:just now",
                "fetched_at": utcnow().isoformat(),
            }
        )

    async def catalog(request):
        calls.append((request.search_text, clock()))
        return object()

    app = web.Application()
    app.router.add_post("/v1/items/details", details)
    async with TestServer(app) as server:
        client = ParserClient(str(server.make_url("")))
        client.pacer = SourcePacer(clock=clock, sleep=clock.sleep)
        monkeypatch.setattr(client, "_parse", catalog)
        item = lot().model_copy(update={"region_id": region})
        try:
            await client.parse(ParseRequest(region_id=region, search_text="Amiri"))
            await client.details(item)
            html = await client.refresh_details(item)
            assert (await client.refresh_details(item)).fetched_at == html.fetched_at
            await client.parse(ParseRequest(region_id=region, search_text="Stone Island"))
            assert calls == [("Amiri", 1000), ("api", 1001), ("html", 1002), ("Stone Island", 1003)]
        finally:
            await client.close()


@pytest.mark.parametrize("region", ["FR", "GB"])
async def test_source_retry_after_survives_client_restart(tmp_path, region):
    path = tmp_path / "cooldowns.json"
    first = ParserClient(state_path=path)
    try:
        first.remember_failure(region, ParseFailure("SourceRateLimited", retry_after=900))
        first.remember_failure(region, ParseFailure("SourceRateLimited", retry_after=30))
    finally:
        await first.close()
    second = ParserClient(state_path=path)
    try:
        with pytest.raises(ParseFailure) as error:
            await second.parse(ParseRequest(region_id=region))
        assert error.value.deferred and 898 <= error.value.retry_after <= 900
        assert second.pacer.next_start == 0  # No HTTP admission during the saved pause.
        second.check_cooldown("DE")
    finally:
        await second.close()


@pytest.mark.parametrize("content", ["[]", '{"version":1,"regions":{"FR":null}}', "invalid"])
async def test_invalid_pause_checkpoint_does_not_remove_rate_limit(tmp_path, content):
    path = tmp_path / "cooldowns.json"
    path.write_text(content, encoding="utf-8")
    client = ParserClient(state_path=path)
    try:
        with pytest.raises(ParseFailure) as error:
            client.check_cooldown("FR")
        assert 118 <= error.value.retry_after <= 120
    finally:
        await client.close()


@pytest.mark.parametrize("region", ["FR", "DE", "IT", "UK"])
async def test_quiet_time_recovers_without_thirty_responses_or_a_new_window(region):
    clock = Clock()
    pacer = SourcePacer(clock=clock, sleep=clock.sleep)
    for _ in range(3):
        pacer.limited(region)
    assert pacer.regions[region] == 16
    clock.now = 1599
    assert not pacer.recover(region)
    assert pacer.regions[region] == 16
    clock.now = 1600
    assert pacer.recover(region) and pacer.regions[region] == 8
    assert pacer.recover_after[region] == 2200
    assert not pacer.recover(region)
    clock.now = 3400
    assert pacer.recover(region)
    assert region not in pacer.regions and region not in pacer.recover_after
    await pacer.acquire(region, lambda _: None)
    await pacer.acquire(region, lambda _: None)
    assert clock() == 3401  # Normal spacing still applies after idle recovery.


async def test_recovery_releases_old_booking_and_refusal_restarts_the_window():
    clock = Clock()
    pacer = SourcePacer(clock=clock, sleep=clock.sleep)
    for _ in range(3):
        pacer.limited("FR")
    clock.now = 1599
    await pacer.acquire("FR", lambda _: None)
    assert pacer.next_region["FR"] == 1615
    clock.now = 1600
    assert await pacer.acquire("FR", lambda _: None)
    assert clock() == 1607  # Eight seconds since the actual last start.
    pacer.limited("FR")
    assert pacer.regions["FR"] == 16
    clock.now = 2200  # The previous recovery deadline no longer applies.
    assert not pacer.recover("FR") and pacer.regions["FR"] == 16
    assert pacer.recover_after["FR"] == 2207


@pytest.mark.parametrize("region,brand", [("FR", "Nike"), ("DE", "Gucci"),
                                        ("IT", "Adidas"), ("UK", "Amiri")])
async def test_saved_old_pause_recovers_for_existing_and_future_queries(tmp_path, region, brand, monkeypatch):
    path = tmp_path / "source-cooldowns.json"
    path.write_text(json.dumps({"version": 1, "regions": {}, "pacing": {region: {
        "interval": 16, "recover_after": (utcnow() - timedelta(hours=2)).isoformat()
    }}}))
    client = ParserClient(state_path=path)
    calls = []

    async def catalog(request):
        calls.append(request.search_text)
        return object()

    monkeypatch.setattr(client, "_parse", catalog)
    try:
        assert region not in client.pacer.regions
        assert json.loads(path.read_text())["pacing"] == {}
        await client.parse(ParseRequest(region_id=region, search_text=brand))
        await client.parse(ParseRequest(region_id=region, search_text="another saved search"))
        assert calls == [brand, "another saved search"]
    finally:
        await client.close()


@pytest.mark.parametrize("region", ["FR", "UK"])
async def test_recovery_deadline_and_idle_progress_survive_repeated_restarts(tmp_path, region):
    path = tmp_path / "source-cooldowns.json"
    deadline = utcnow() - timedelta(seconds=500)
    path.write_text(json.dumps({"version": 1, "regions": {}, "pacing": {region: {
        "interval": 16, "recover_after": deadline.isoformat()
    }}}))
    expected = deadline + timedelta(seconds=600)
    for _ in range(3):
        client = ParserClient(state_path=path)
        try:
            assert client.pacer.regions[region] == 8  # No successful responses needed.
            saved = json.loads(path.read_text())["pacing"][region]
            from datetime import datetime
            assert abs((datetime.fromisoformat(saved["recover_after"]) - expected).total_seconds()) < 0.1
        finally:
            await client.close()

    client = ParserClient(state_path=path)
    client.pacer.regions[region] = 16
    client.pacer.recover_after[region] = monotonic() - 1801
    await client.close()  # Persisting while stopped must not replace a past deadline with now.
    restarted = ParserClient(state_path=path)
    try:
        assert region not in restarted.pacer.regions
    finally:
        await restarted.close()


@pytest.mark.parametrize("region", ["FR", "UK"])
async def test_old_pacing_cannot_shorten_an_active_retry_after(tmp_path, region):
    path = tmp_path / "source-cooldowns.json"
    path.write_text(json.dumps({"version": 1,
        "regions": {region: {"code": "SourceRateLimited", "until": (utcnow() + timedelta(seconds=900)).isoformat()}},
        "pacing": {region: {"interval": 16, "recover_after": (utcnow() - timedelta(hours=2)).isoformat()}}}))
    for _ in range(2):
        client = ParserClient(state_path=path)
        try:
            assert client.pacer.regions[region] == 16
            with pytest.raises(ParseFailure) as error:
                await client.parse(ParseRequest(region_id=region))
            assert error.value.code == "SourceRateLimited" and error.value.retry_after >= 898
            assert client.pacer.next_start == 0
            client.check_cooldown("IT")
        finally:
            await client.close()


async def test_budget_refusals_do_not_create_or_restart_source_recovery(tmp_path):
    client = ParserClient(state_path=tmp_path / "source-cooldowns.json")
    try:
        client.pacer.limited("FR")
        deadline = client.pacer.recover_after["FR"]
        for code in ("ProxyBudgetWait", "ProxyBudgetNotStarted", "ProxyBudgetExceeded", "ParserBusy"):
            client.remember_failure("FR", ParseFailure(code, retry_after=30))
        assert client.pacer.recover_after["FR"] == deadline
        assert not client.cooldowns
    finally:
        await client.close()


@pytest.mark.parametrize("region,brand", [("FR", "Nike"), ("DE", "Gucci"),
                                        ("IT", "Adidas"), ("UK", "Amiri")])
async def test_actual_detail_budget_refusal_does_not_pause_affordable_catalog(region, brand, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("vpars_worker.parser.monotonic", clock)
    calls = []

    async def details(request):
        calls.append("details")
        return web.json_response({"error": {"code": "ProxyBudgetWait", "retry_after": 90}}, status=503)

    async def catalog(request):
        calls.append(request.search_text)
        return object()

    app = web.Application()
    app.router.add_post("/v1/items/details", details)
    async with TestServer(app) as server:
        client = ParserClient(str(server.make_url("")))
        client.pacer = SourcePacer(clock=clock, sleep=clock.sleep)
        monkeypatch.setattr(client, "_parse", catalog)
        try:
            with pytest.raises(ParseFailure):
                await client.details(lot().model_copy(update={"region_id": region}))
            with pytest.raises(ParseFailure):
                client.check_cooldown("DE", "details")
            await client.parse(ParseRequest(region_id=region, search_text=brand))
            assert calls == ["details", brand]
            assert not client.cooldowns and not client.pacer.regions
        finally:
            await client.close()


@pytest.mark.parametrize("kind,other", [("catalog", "details"), ("details", "catalog")])
async def test_budget_wait_is_per_kind_but_real_source_pause_still_covers_both(kind, other, monkeypatch):
    clock = Clock()
    monkeypatch.setattr("vpars_worker.parser.monotonic", clock)
    client = ParserClient()
    try:
        client.remember_failure("FR", ParseFailure("ProxyBudgetWait", retry_after=30), kind)
        for region in ("FR", "IT", "DE", "GB"):
            client.check_cooldown(region, other)
            with pytest.raises(ParseFailure):
                client.check_cooldown(region, kind)
        client.remember_failure("FR", ParseFailure("SourceRateLimited", retry_after=120), kind)
        for lane in (kind, other):
            with pytest.raises(ParseFailure) as caught:
                client.check_cooldown("FR", lane)
            assert caught.value.code == "SourceRateLimited"
        clock.now += 3
        client.check_cooldown("DE", kind)
    finally:
        await client.close()
