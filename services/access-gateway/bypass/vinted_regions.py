"""The four enabled Vinted markets. Poland was disabled by request on 2026-09-22.

Source: https://www.vinted.fr/catalog (country links in rendered HTML).
UK is retained for compatibility with the project's original region codes;
the ISO country code GB is accepted as an alias.
"""

from dataclasses import dataclass
from urllib.parse import urlencode


@dataclass(frozen=True)
class Region:
    code: str
    name: str
    domain: str

    @property
    def base_url(self) -> str:
        return f"https://www.{self.domain}"

    @property
    def hosts(self) -> tuple[str, str]:
        return self.domain, "www." + self.domain

    def catalog_url(self, search_text: str = "", page: int = 1, newest_first: bool = False) -> str:
        if type(page) is not int or page < 1:
            raise ValueError("Page must be a positive integer")
        params = {}
        if search_text:
            params["search_text"] = search_text
        if page != 1:
            params["page"] = str(page)
        if newest_first:
            params["order"] = "newest_first"
        return self.base_url + "/catalog" + ("?" + urlencode(params) if params else "")


REGIONS = {region.code: region for region in (
    Region("UK", "Велика Британія", "vinted.co.uk"),
    Region("FR", "Франція", "vinted.fr"),
    Region("IT", "Італія", "vinted.it"),
    Region("DE", "Німеччина", "vinted.de"),
)}


def get_region(code: str) -> Region:
    normalized = code.strip().upper()
    if normalized == "GB":
        normalized = "UK"
    try:
        return REGIONS[normalized]
    except KeyError:
        raise ValueError(f"Unknown Vinted region: {code}. Use --list-regions.") from None


def select_regions(codes: list[str]) -> list[Region]:
    tokens = [token.strip() for code in codes for token in code.split(",") if token.strip()]
    if not tokens:
        raise ValueError("Select at least one region")
    if len(tokens) == 1 and tokens[0].lower() == "all":
        return list(REGIONS.values())
    # Retain the requested order and avoid duplicate live requests.
    return list({region.code: region for region in map(get_region, tokens)}.values())
