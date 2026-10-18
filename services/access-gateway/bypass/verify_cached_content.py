"""Read-only live gateway/parser check; never touches searches or Telegram."""

import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "parser"))

from vinted import fetch_catalog
from vpars_parser.models import ItemDetailsRequest
from vpars_parser.ports import GatewayPage, GatewayQuery
from vpars_parser.sources.vinted import VintedAdapter


def page(result):
    return GatewayPage(result.region, result.url, result.final_url,
                       result.outcome.upper(), result.body.encode(), result.solution_status)


def main():
    adapter = VintedAdapter()
    failed = False
    for code, brand in (("DE", "Nike"), ("FR", "Adidas"), ("IT", "Stone Island"), ("UK", "Amiri")):
        result = fetch_catalog(code, search_text=brand, timeout=20, newest_first=True)
        if result.outcome != "page_loaded":
            print(json.dumps({"region": code, "outcome": result.outcome,
                              "retry_after": result.retry_after}), flush=True)
            failed = True
            continue
        query = GatewayQuery(code, brand, 1, 20, "traffic-verification", newest_first=True)
        adapter.validate_response(page(result), query)
        parsed = adapter.parse_page(result.body, query)
        items = [adapter.normalize_item(record, code, datetime.now(timezone.utc))
                 for record in parsed.records]
        print(json.dumps({"region": code, "brand": brand, "parsed_items": len(items),
                          "identities_complete": parsed.identities_complete,
                          "unrecognized_cards": parsed.unrecognized_cards,
                          "with_photo_urls": sum(bool(item.photo_urls) for item in items),
                          "pagination": parsed.pagination.has_more}), flush=True)
        assert parsed.identities_complete and not parsed.unrecognized_cards and items
        assert all(item.photo_urls for item in items)
        # Test full item data for two different markets, using the existing parser.
        if code in ("DE", "IT"):
            time.sleep(4)
            request = ItemDetailsRequest(region_id=code, source_item_id=items[0].source_item_id)
            details_page = fetch_catalog(code, item_id=request.source_item_id, timeout=20)
            if details_page.outcome != "page_loaded":
                print(json.dumps({"region": code, "details_outcome": details_page.outcome,
                                  "retry_after": details_page.retry_after}), flush=True)
                failed = True
            else:
                adapter.validate_item_response(page(details_page), request)
                details = adapter.parse_details(details_page.body, request, datetime.now(timezone.utc))
                print(json.dumps({"region": code, "details_photos": len(details.photo_urls),
                                  "photos_complete": details.photos_complete,
                                  "description_present": details.description is not None,
                                  "seller_present": details.seller is not None,
                                  "publication_evidence": details.publication_evidence}), flush=True)
                assert details.photos_complete and details.photo_urls
        time.sleep(4)
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
