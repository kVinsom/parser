"""Public Vinted catalogs through the local Byparr browser service.

One request per selected region, with content validation and incremental reports.
Import fetch_catalog/get_region for reuse; cookies and sessions are not persisted.
"""

from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import time
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlsplit

from artifacts import save_html
from cancellable_http import RequestCancelled
from vinted_regions import REGIONS, Region, get_region, select_regions

ROOT = Path(__file__).resolve().parent


@dataclass
class CatalogResult:
    region: str
    url: str
    outcome: str = "service_error"
    reason: str = "Byparr did not return a valid solution"
    title: str = ""
    final_url: str = ""
    solution_status: int | None = None
    byparr_http_status: int | None = None
    byparr_version: str = ""
    browser_mode: str = "unreported"
    browser_lifecycle: str = "unreported"
    block_media: bool = True
    item_count: int = 0
    item_link_count: int = 0
    item_samples: list[str] = field(default_factory=list)
    challenge_sources: list[str] = field(default_factory=list)
    response_bytes: int = 0
    cookie_count: int = 0
    elapsed_seconds: float = 0
    timings_ms: dict[str, float] = field(default_factory=dict)
    body: str = field(default="", repr=False)
    retry_after: int | None = None

    def summary(self) -> dict:
        return {key: value for key, value in vars(self).items() if key != "body"}


def retry_after_seconds(value, now=None):
    if not isinstance(value, str) or len(value) > 100:
        return None
    try:
        if value.strip().isdigit():
            return max(1, min(int(value), 86400))
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is None:
            return None
        return max(1, min(math.ceil((stamp - (now or datetime.now(timezone.utc))).total_seconds()), 86400))
    except (ValueError, TypeError, OverflowError):
        return None


class PageSignals(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title: list[str] = []
        self.text: list[str] = []
        self.links: list[str] = []
        self.sources: list[str] = []
        self.in_title = False
        self.hidden = False
        self.empty_markers: set[str] = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if not self.hidden and attrs.get("data-testid") in {
            "search-empty-state", "search-empty-state--title", "search-empty-state--body",
        }:
            self.empty_markers.add(attrs["data-testid"])
        if tag == "title":
            self.in_title = True
        if tag in ("script", "style"):
            self.hidden = True
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])
        if tag in ("iframe", "script") and attrs.get("src"):
            self.sources.append(attrs["src"])

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if tag in ("script", "style"):
            self.hidden = False

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        if not self.hidden:
            self.text.append(data)


def is_region_url(url: str, region: Region) -> bool:
    try:
        parsed = urlsplit(url)
        return (parsed.scheme == "https" and parsed.hostname in region.hosts
                and parsed.port in (None, 443) and parsed.username is None
                and parsed.password is None)
    except ValueError:
        return False


def public_url(url: str) -> str:
    """Keep only host/path; redirect query strings can contain challenge tokens."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            return ""
        return f"{parsed.scheme}://{parsed.hostname}{parsed.path}"
    except ValueError:
        return ""


def classify(result: CatalogResult, region: Region, final_url: str, status: int,
             body: str, *, item_id: str | None = None) -> CatalogResult:
    result.body = body
    result.final_url = public_url(final_url)
    result.solution_status = status
    result.response_bytes = len(body.encode("utf-8"))
    signals = PageSignals()
    signals.feed(body)
    result.title = " ".join("".join(signals.title).split())[:240]
    for source in signals.sources:
        source_url = public_url(urljoin(region.base_url, source))
        host = urlsplit(source_url).hostname or ""
        if host == "captcha-delivery.com" or host.endswith(".captcha-delivery.com"):
            result.challenge_sources.append(source_url)
    visible = " ".join(signals.text).lower()
    if status == 429:
        result.outcome, result.reason = "rate_limited", "Vinted returned a rate limit"
    elif status in (401, 403) or result.challenge_sources or "cf-chl-" in body or any(
        phrase in visible for phrase in (
            "verify you are human", "enable js and disable any ad blocker",
            "access denied", "unusual traffic", "vérifiez que vous êtes humain",
        )
    ):
        result.outcome, result.reason = "blocked", "Access denied or an anti-bot challenge remains"
    elif not is_region_url(final_url, region):
        result.outcome, result.reason = "unexpected_redirect", "Final URL left the requested region"
    elif item_id is not None and not re.match(
        r"^/items/" + re.escape(item_id) + r"(?:[-/]|$)", urlsplit(final_url).path
    ):
        result.outcome, result.reason = "unexpected_redirect", "Final URL is not the requested item"
    elif item_id is None and urlsplit(final_url).path.rstrip("/") != "/catalog":
        result.outcome, result.reason = "unexpected_redirect", "Final URL is not the requested catalog"
    elif item_id is None and catalog_context(result.url) != catalog_context(final_url):
        result.outcome, result.reason = "unexpected_redirect", "Final catalog query differs from the request"
    elif not 200 <= status < 300:
        result.outcome, result.reason = "upstream_error", "Vinted returned an unsuccessful status"
    elif item_id is not None:
        if 'data-testid="item-photo-' in body and "vinted" in result.title.lower():
            result.outcome, result.reason = "page_loaded", "Requested item page contains a gallery"
        else:
            result.outcome, result.reason = "unexpected_content", "Item page could not be confirmed"
    else:
        items: dict[str, str] = {}
        for link in signals.links:
            try:
                absolute = urljoin(region.base_url, link)
                if not is_region_url(absolute, region):
                    continue
                match = re.match(r"^/items/(\d+)(?:[-/]|$)", urlsplit(absolute).path)
                if match:
                    result.item_link_count += 1
                    items.setdefault(match[1], public_url(absolute))
            except ValueError:
                continue
        result.item_count = len(items)
        result.item_samples = list(items.values())[:5]
        confirmed_empty = len(signals.empty_markers) == 3
        if "vinted" in result.title.lower() and not items and confirmed_empty:
            result.outcome, result.reason = "page_loaded", "Requested catalog explicitly reports no results"
        elif "vinted" in result.title.lower() and items and not confirmed_empty:
            result.outcome, result.reason = "page_loaded", "Requested regional catalog contains item links"
        else:
            result.outcome, result.reason = "unexpected_content", "Catalog with items could not be confirmed"
    return result


def catalog_context(url: str):
    """Check actual redirects before stripping URL queries from public metadata.

    The requested sort parameter is a routing contract, not proof of item age.
    Ignore tracking/challenge keys, but never accept another offset or search.
    """
    params = parse_qs(urlsplit(url).query, keep_blank_values=True)
    return (params.get("search_text", [""]), params.get("page", ["1"]), params.get("order", [""]))


def validate_endpoint(endpoint: str) -> str:
    try:
        parsed = urlsplit(endpoint)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.query or parsed.fragment or parsed.path not in ("", "/")
                or parsed.port == 0):
            raise ValueError
    except ValueError:
        raise ValueError("Byparr URL must be a base HTTP(S) address without credentials or a path") from None
    return endpoint.rstrip("/")


def fetch_catalog(region: Region | str, *, endpoint: str = "http://127.0.0.1:8191",
                  timeout: int = 60, search_text: str = "", page: int = 1,
                  block_media: bool = True, measure_timing: bool = False,
                  newest_first: bool = False, item_id: str | None = None,
                  cancellation=None) -> CatalogResult:
    """Return validated public catalog HTML and metadata, never persisted cookies."""
    if isinstance(region, str):
        region = get_region(region)
    if region not in REGIONS.values():
        raise ValueError("Region must be in the Vinted registry")
    if type(timeout) is not int or not 1 <= timeout <= 300:
        raise ValueError("Timeout must be 1 to 300 seconds")
    if type(block_media) is not bool:
        raise ValueError("block_media must be a boolean")
    if type(measure_timing) is not bool:
        raise ValueError("measure_timing must be a boolean")
    endpoint = validate_endpoint(endpoint)
    if item_id is not None and not re.fullmatch(r"[1-9][0-9]{0,19}", item_id):
        raise ValueError("Invalid item identity")
    url = (region.base_url + "/items/" + item_id if item_id is not None
           else region.catalog_url(search_text, page, newest_first))
    from curl_cffi import requests

    result = CatalogResult(region.code, url, block_media=block_media)
    started = time.monotonic()
    try:
        http_started = time.monotonic()
        try:
            payload = {"cmd": "request.get", "url": url, "maxTimeout": timeout * 1000,
                       "returnOnlyCookies": False, "blockMedia": block_media}
            headers = {"X-Vinted-Measure-Timing": "1"} if measure_timing else {}
            if cancellation is not None:
                response = cancellation.post(endpoint + "/v1", payload=payload,
                                             timeout=timeout + 10, headers=headers)
            else:
                with requests.Session(trust_env=False) as session:
                    response = session.post(endpoint + "/v1", json=payload,
                                            timeout=timeout + 10, allow_redirects=False, headers=headers)
        finally:
            result.timings_ms["byparr_http_ms"] = (time.monotonic() - http_started) * 1000
        result.byparr_http_status = response.status_code
        if response.status_code == 503:
            from proxy_budget import CODES
            code = response.headers.get("X-Vinted-Budget-Code")
            if code in CODES:
                result.reason = code
                result.retry_after = retry_after_seconds(response.headers.get("X-Vinted-Retry-After")) or 30
                return result
        mode = response.headers.get("X-Vinted-Catalog-Mode", "unreported")
        if mode in ("ready-content", "standard"):
            result.browser_mode = mode
        lifecycle = response.headers.get("X-Vinted-Browser-Lifecycle", "unreported")
        if lifecycle in ("reused", "per-request"):
            result.browser_lifecycle = lifecycle
        if measure_timing:
            try:
                timing_data = json.loads(response.headers.get("X-Vinted-Timings", "{}"))
                if isinstance(timing_data, dict):
                    for key in ("browser_navigation_ms", "browser_document_request_ms", "browser_ttfb_ms"):
                        value = timing_data.get(key)
                        if type(value) in (int, float) and math.isfinite(value) and 0 <= value <= 360000:
                            result.timings_ms[key] = value
            except (ValueError, TypeError):
                pass  # Optional diagnostics must not change the access result.
        result.retry_after = retry_after_seconds(response.headers.get("X-Vinted-Retry-After"))
        upstream_status = response.headers.get("X-Vinted-Upstream-Status", "")
        if isinstance(upstream_status, str) and re.fullmatch(r"[45][0-9]{2}", upstream_status):
            result.solution_status = int(upstream_status)
            if result.solution_status == 429:
                result.outcome, result.reason = "rate_limited", "Vinted rate limited access"
            elif result.solution_status in (401, 403):
                result.outcome, result.reason = "blocked", "Vinted access was refused"
            else:
                result.outcome, result.reason = "upstream_error", "Vinted returned an error"
            return result
        if response.status_code != 200:
            if response.status_code in (408, 504):
                result.outcome, result.reason = "timeout", "Byparr browser request timed out"
            else:
                result.reason = "Byparr API returned an unsuccessful status"
            return result
        decode_started = time.monotonic()
        payload = response.json()
        result.timings_ms["json_decode_ms"] = (time.monotonic() - decode_started) * 1000
        solution = payload.get("solution") if isinstance(payload, dict) else None
        if (not isinstance(payload, dict) or payload.get("status") != "ok"
                or not isinstance(solution, dict)
                or type(solution.get("status")) is not int
                or not isinstance(solution.get("url"), str)
                or not isinstance(solution.get("response"), str)
                or not isinstance(solution.get("cookies", []), list)):
            return result
        result.cookie_count = len(solution.get("cookies", []))
        result.retry_after = retry_after_seconds(response.headers.get("X-Vinted-Retry-After"))
        if result.retry_after is None and isinstance(solution.get("headers"), dict):
            result.retry_after = next((
                retry_after_seconds(value) for key, value in solution["headers"].items()
                if key.casefold() == "retry-after"
            ), None)
        if isinstance(payload.get("version"), str):
            result.byparr_version = payload["version"]
        # Byparr 3.0.4 may return status 200 for a challenge: inspect actual HTML.
        validation_started = time.monotonic()
        try:
            return classify(result, region, solution["url"], solution["status"], solution["response"],
                            item_id=item_id)
        finally:
            result.timings_ms["catalog_validation_ms"] = (time.monotonic() - validation_started) * 1000
    except RequestCancelled:
        result.reason = "Browser request was cancelled by its caller"
    except (requests.exceptions.Timeout, TimeoutError):
        result.outcome, result.reason = "timeout", "Byparr request timed out"
    except (requests.exceptions.RequestException, OSError, http.client.HTTPException):
        result.reason = "Cannot connect to Byparr; check Docker and the service address"
    except (ValueError, TypeError, AttributeError):
        result.reason = "Malformed Byparr response"
    finally:
        elapsed = time.monotonic() - started
        result.elapsed_seconds = round(elapsed, 3)
        result.timings_ms["fetch_total_ms"] = elapsed * 1000
    return result


def write_reports(folder: Path, report: dict) -> None:
    report["counts"] = dict(Counter(row["outcome"] for row in report["results"]))
    report["completed"] = len(report["results"])
    report["all_accessible"] = (report["completed"] == len(report["requested_regions"])
                                and report["counts"].get("page_loaded", 0) == report["completed"])
    temporary = folder / "report.json.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(folder / "report.json")
    lines = ["# Перевірка регіонів Vinted", "", f"Час початку (UTC): {report['started_at']}", "",
             f"Завершено: {report['completed']}/{len(report['requested_regions'])}. "
             f"Каталог із товарами: {report['counts'].get('page_loaded', 0)}.", "",
             "| Регіон | Каталог | Результат | Унікальні товари на сторінці | Секунди |",
             "|---|---|---|---:|---:|"]
    for row in report["results"]:
        region = REGIONS[row["region"]]
        lines.append(f"| {region.code} — {region.name} | [{region.domain}]({region.base_url}/catalog) "
                     f"| {row['outcome']} | {row['item_count']} | {row['elapsed_seconds']} |")
    lines.extend(["", "Це одноразова перевірка публічних каталогів через поточне з'єднання.",
                  "HTTP 200 без товарів не зараховується як успіх. Кількість товарів стосується",
                  "отриманої сторінки, а не всього каталогу. Вхід, купівля, доставка та API не перевірялися.",
                  "Мова браузера й проксі визначаються конфігурацією контейнера Byparr.", ""])
    (folder / "report.md").write_text("\n".join(lines), encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check Vinted regional catalogs through Byparr")
    parser.add_argument("--regions", "--region", nargs="+", default=["FR"], metavar="CODE",
                        help="Country codes, comma-separated or spaced; all checks enabled regions: "
                             + ", ".join(REGIONS))
    parser.add_argument("--list-regions", action="store_true")
    parser.add_argument("--byparr-url", help="Byparr base address; defaults to BYPARR_URL or localhost:8191")
    parser.add_argument("--timeout", type=int, default=60)
    parser.add_argument("--search-text", default="")
    parser.add_argument("--page", type=int, default=1)
    parser.add_argument("--save-html", action="store_true")
    parser.add_argument("--load-media", action="store_true", help="Load images, media and fonts (slower diagnostic mode)")
    args = parser.parse_args(argv)
    if args.list_regions:
        print(json.dumps([{**vars(region), "base_url": region.base_url}
                          for region in REGIONS.values()], ensure_ascii=True, indent=2))
        return 0
    try:
        regions = select_regions(args.regions)
        if not 1 <= args.timeout <= 300 or args.page < 1:
            raise ValueError("Timeout must be 1 to 300 seconds and page must be positive")
        local_env = {}
        if (ROOT / ".env").is_file():
            from dotenv import dotenv_values
            local_env = dotenv_values(ROOT / ".env", interpolate=False)
        endpoint = validate_endpoint(args.byparr_url or os.environ.get("BYPARR_URL")
                                     or local_env.get("BYPARR_URL") or "http://127.0.0.1:8191")
        # Import the binary module too, so missing native dependencies fail before a report.
        import curl_cffi  # noqa: F401
    except ValueError as error:
        parser.error(str(error))
    except ImportError:
        parser.error("Install requirements.txt or run through run.ps1")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    folder = ROOT / ".artifacts" / ("vinted-regions-" + stamp + "-" + uuid.uuid4().hex[:8])
    folder.mkdir(parents=True)
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "transport": "byparr",
              "requested_regions": [r.code for r in regions], "results": [], "interrupted": False,
              "search_text": args.search_text, "page": args.page,
              "block_media": not args.load_media,
              "report_path": str(folder / "report.json")}
    write_reports(folder, report)
    print(f"REPORT {folder / 'report.md'}", flush=True)
    try:
        for region in regions:
            result = fetch_catalog(region, endpoint=endpoint, timeout=args.timeout,
                                   search_text=args.search_text, page=args.page, block_media=not args.load_media)
            row = result.summary()
            if args.save_html and result.body:
                html_file = folder / (region.code + ".html.gz")
                save_html(html_file, result.body)
                row["html_path"] = str(html_file)
            report["results"].append(row)
            write_reports(folder, report)
            print(f"{len(report['results']):02}/{len(regions):02} {region.code}: {result.outcome}; "
                  f"items={result.item_count}; {result.elapsed_seconds}s", flush=True)
    except KeyboardInterrupt:
        report["interrupted"] = True
        write_reports(folder, report)
        return 130
    print(json.dumps({key: report[key] for key in ("completed", "counts", "all_accessible", "report_path")},
                     ensure_ascii=True, indent=2), flush=True)
    return 0 if report["all_accessible"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
