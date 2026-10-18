"""Skip nonessential interface traffic on public Vinted pages.

No documents, catalog/item API, application scripts, consent UI or access checks
are blocked. This policy is scoped to the same four public markets as the bot.
"""

from urllib.parse import urlsplit

from browser_network import BACKGROUND_DOWNLOAD_HOSTS, source_url
from vinted_regions import REGIONS

MARKET_HOSTS = frozenset(host for region in REGIONS.values() for host in region.hosts)
ANALYTICS_HOSTS = frozenset({"metrics.vinted.lt", "www.google-analytics.com",
                             "www.googletagmanager.com", "region1.google-analytics.com"})
INTERFACE_API_PATHS = frozenset({"/api/v2/banners", "/api/v2/info_banners/catalog",
                                 "/api/v2/promoted_closets"})


def should_skip(request):
    try:
        url = urlsplit(request.url)
        allowed_origin = (url.scheme == "https" and not url.username
                          and not url.password and url.port in (None, 443))
    except ValueError:
        return False
    if not allowed_origin:
        return False
    if url.hostname in BACKGROUND_DOWNLOAD_HOSTS:
        return True
    if request.resource_type in {"image", "media", "font"}:
        return True
    if request.resource_type not in {"script", "xhr", "fetch", "beacon", "ping", "image"}:
        return False
    if url.hostname in ANALYTICS_HOSTS:
        return True
    # Observed /gtg scripts identify themselves as Google Tag Manager/Analytics.
    # Match a path boundary, never the application or an unrelated similar host.
    if url.hostname in MARKET_HOSTS and url.path.startswith("/gtg/"):
        return True
    if (url.hostname in MARKET_HOSTS and request.resource_type in {"xhr", "fetch"}
            and request.method == "GET" and url.path.rstrip("/") in INTERFACE_API_PATHS):
        return True
    headers = request.headers
    return (url.hostname in MARKET_HOSTS and request.method == "GET"
            and request.resource_type in {"fetch", "xhr"}
            and (headers.get("next-router-prefetch") == "1"
                 or headers.get("purpose", "").lower() == "prefetch"
                 or "prefetch" in {part.strip() for part in
                                   headers.get("sec-purpose", "").lower().split(";")}))


def install_traffic_policy(endpoints, *, skip_analytics=True):
    original = endpoints.setup_routes

    async def setup(request, dep):
        await original(request, dep)
        async def route_request(route):
            # Page routes run before context routes; this guard must remain
            # ahead of Byparr's continue_() even when analytics filtering is off.
            if not source_url(route.request.url) or (skip_analytics and should_skip(route.request)):
                await route.abort()
            else:
                await route.fallback()

        await dep.page.route("**/*", route_request)

    endpoints.setup_routes = setup
