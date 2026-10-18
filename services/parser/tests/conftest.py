from dataclasses import replace
from pathlib import Path
from urllib.parse import urlencode

import pytest
from selectolax.parser import HTMLParser

from vpars_parser.models import Region
from vpars_parser.ports import GatewayPage
from vpars_parser.sources.vinted import DOMAINS

FIXTURES = Path(__file__).parent / "fixtures"


def html(region="UK"):
    return (FIXTURES / f"{region}.html").read_text(encoding="utf-8")


def first_card(region="UK"):
    tree = HTMLParser(html(region))
    return tree.css_first('[data-testid="product-item-id-10001"]').html


class FakeGateway:
    def __init__(self):
        self.calls = []
        self.region_calls = 0
        self.regions = [
            Region(code=r, name=r, base_url=f"https://www.{d}") for r, d in DOMAINS.items()
        ]
        self.changes = {}
        self.error = None
        self.closed = False
        self.started = None
        self.release = None

    async def list_regions(self, request_id):
        self.region_calls += 1
        return self.regions

    async def fetch_catalog(self, query):
        self.calls.append(query)
        if self.started:
            self.started.set()
        if self.release:
            await self.release.wait()
        if self.error:
            raise self.error
        base = f"https://www.{DOMAINS[query.region]}/catalog"
        params = {}
        if query.search_text:
            params["search_text"] = query.search_text
        if query.page != 1:
            params["page"] = str(query.page)
        if query.newest_first:
            params["order"] = "newest_first"
        page = GatewayPage(
            region=query.region,
            url=base + ("?" + urlencode(params) if params else ""),
            final_url=base,
            outcome="PAGE_LOADED",
            html=html(query.region).encode(),
            upstream_status=200,
        )
        return replace(page, **self.changes)

    async def close(self):
        self.closed = True


@pytest.fixture
def gateway():
    return FakeGateway()
