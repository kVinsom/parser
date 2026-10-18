"""Only source traffic uses the upstream; essential browser services go direct."""

from contextlib import asynccontextmanager
from functools import wraps
from urllib.parse import urlsplit

from source_proxy import allowed_target

BACKGROUND_DOWNLOAD_HOSTS = (
    "edgedl.me.gvt1.com",
    "firefox-settings-attachments.cdn.mozilla.net",
)

# Native browser requests are invisible to Playwright routes. Keep security
# services available directly; ordinary pages cannot use this bypass because
# every context gets a separate source-only request filter below. Unknown
# services remain blocked by SourceProxy, never forwarded to the paid proxy.
DIRECT_SERVICE_DOMAINS = (
    ".mozilla.com", ".mozilla.org", ".mozilla.net", ".firefox.com",
    ".digicert.com", ".sectigo.com", ".pki.goog", ".globalsign.com",
    ".globalsign.net", ".letsencrypt.org", ".lencr.org", ".amazontrust.com",
    "safebrowsing.googleapis.com", "safebrowsing.google.com",
)

COMPONENT_PREFS = {
    "media.gmp-manager.updateEnabled": False,
    "media.gmp-widevinecdm.enabled": False,
    "media.gmp-widevinecdm.autoupdate": False,
    "media.gmp-gmpopenh264.enabled": False,
    "media.gmp-gmpopenh264.autoupdate": False,
    "media.eme.enabled": False,
    "browser.ml.enable": False,
    "browser.translations.enable": False,
    "app.update.enabled": False,
    "app.update.auto": False,
    "extensions.update.enabled": False,
    "extensions.systemAddon.update.enabled": False,
    "toolkit.telemetry.enabled": False,
    "toolkit.telemetry.unified": False,
    "datareporting.healthreport.uploadEnabled": False,
    "app.update.service.enabled": False,
    "app.update.disabledForTesting": True,
    "app.normandy.enabled": False,
    "app.normandy.api_url": "",
    "app.shield.optoutstudies.enabled": False,
    "browser.startup.homepage": "about:blank",
    "browser.startup.page": 0,
    "browser.newtabpage.enabled": False,
    "browser.newtabpage.activity-stream.feeds.telemetry": False,
    "browser.newtabpage.activity-stream.feeds.topsites": False,
    "browser.newtabpage.activity-stream.feeds.section.topstories": False,
    "browser.newtabpage.activity-stream.showSponsoredTopSites": False,
    "browser.topsites.contile.enabled": False,
    "browser.region.update.enabled": False,
    "browser.region.network.url": "",
    "browser.search.update": False,
    "browser.search.suggest.enabled": False,
    "dom.push.enabled": False,
    "dom.push.connection.enabled": False,
    "dom.push.serverURL": "",
    "network.prefetch-next": False,
    "network.dns.disablePrefetch": True,
    "network.dns.disablePrefetchFromHTTPS": True,
    "network.http.speculative-parallel-limit": 0,
    "network.predictor.enabled": False,
    "network.captive-portal-service.enabled": False,
    "network.connectivity-service.enabled": False,
    "network.trr.mode": 5,
    "network.proxy.failover_direct": False,
}


def include_hosts(value, additions):
    hosts = [host.strip() for host in (value or "").split(",") if host.strip()]
    return ",".join(dict.fromkeys([*hosts, *additions]))


def source_url(value):
    try:
        url = urlsplit(value)
        if url.scheme in {"about", "data", "blob"}:
            return True  # No external connection.
        return (url.scheme == "https" and not url.username and not url.password
                and bool(url.hostname) and allowed_target(url.hostname, url.port or 443))
    except ValueError:
        return False


async def source_route(route):
    if source_url(route.request.url):
        await route.fallback()
    else:
        await route.abort()


def protected_factory(factory):
    """Apply the same policy to pooled, per-request and explicit-proxy browsers."""

    @wraps(factory)
    def create(*args, **kwargs):
        prefs = {**(kwargs.get("extra_prefs") or {}), **COMPONENT_PREFS}
        # Firefox maps these exact names to loopback; they are never resolved
        # to vendor servers, even without a proxy. No broader Mozilla/Google
        # blocklist, certificate-validation changes or TLS exceptions are used.
        prefs["network.dns.localDomains"] = include_hosts(
            prefs.get("network.dns.localDomains"), BACKGROUND_DOWNLOAD_HOSTS)
        bypass = (*BACKGROUND_DOWNLOAD_HOSTS, *DIRECT_SERVICE_DOMAINS)
        prefs["network.proxy.no_proxies_on"] = include_hosts(
            prefs.get("network.proxy.no_proxies_on"), bypass)
        kwargs["extra_prefs"] = prefs
        proxy = kwargs.get("proxy")
        if proxy:
            # Playwright rewrites HTTP proxy prefs from this dict. SOCKS uses
            # the native no_proxies_on pref above in invisible-core instead.
            kwargs["proxy"] = {
                **proxy,
                "bypass": include_hosts(proxy.get("bypass"), bypass),
            }
        return factory(*args, **kwargs)

    return create


def install_browser_network_policy(utils, *, budget=None):
    import invisible_core
    import invisible_playwright.async_api as api
    from source_proxy import SourceProxy

    policy = SourceProxy(budget=budget)
    factory = protected_factory(utils.InvisiblePlaywright)
    prepare_geo = api.prepare_session_geo
    resolve_locale = invisible_core.resolve_session_locale
    from host_geo import HostGeoCache

    host_geo = HostGeoCache(prepare_geo)

    def direct_geo(timezone, proxy):
        # Service lookups and GeoIP downloads must never use the paid endpoint.
        # With an explicit timezone this also avoids an unnecessary IP lookup.
        return host_geo.resolve(timezone)

    api.prepare_session_geo = direct_geo

    def direct_locale(egress_ip, proxy):
        return resolve_locale(None, None)

    invisible_core.resolve_session_locale = direct_locale

    @asynccontextmanager
    async def browser(*args, **kwargs):
        token, proxy = policy.register(kwargs.get("proxy"))
        try:
            async with factory(*args, **{**kwargs, "proxy": proxy}) as instance:
                new_context = instance.new_context

                async def source_context(*context_args, **context_options):
                    context_options["service_workers"] = "block"
                    context = await new_context(*context_args, **context_options)
                    await context.route("**/*", source_route)
                    return context

                instance.new_context = source_context
                yield instance
        finally:
            policy.unregister(token)

    utils.InvisiblePlaywright = browser

    def restore_geo():
        api.prepare_session_geo = prepare_geo
        invisible_core.resolve_session_locale = resolve_locale

    policy.restore_geo = restore_geo
    return policy


def bind_network_lifespan(app, policy):
    original = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        try:
            async with original(application):
                yield
        finally:
            policy.close()
            policy.restore_geo()

    app.router.lifespan_context = lifespan
