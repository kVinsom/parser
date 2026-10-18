"""Read-only live check; never starts a user search or sends a Telegram message."""

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter

from vpars_parser.gateway import GrpcGatewayClient
from vpars_parser.models import ItemDetailsRequest, ParseRequest
from vpars_parser.service import ParserService
from vpars_parser.settings import Settings

DESTINATION = Path(__file__).resolve().parents[1] / ".artifacts/galleries-live-20260924.json"


async def main():
    settings = Settings()
    service = ParserService(GrpcGatewayClient(settings), settings)
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "telegram_messages_sent": 0,
        "regions": [],
    }
    try:
        for region in ("GB", "FR", "IT", "DE"):
            row = {"region": region}
            started = perf_counter()
            try:
                catalog = await service.parse(
                    ParseRequest(region_id=region, search_text="nike", newest_first=True)
                )
                row.update(
                    catalog_items=len(catalog.items),
                    catalog_ms=round((perf_counter() - started) * 1000, 1),
                )
                item = catalog.items[0]
                started = perf_counter()
                details = await service.details(
                    ItemDetailsRequest(
                        region_id=region, source_item_id=item.source_item_id, timeout_seconds=30
                    )
                )
                row.update(
                    item_id=item.source_item_id,
                    photo_count=len(details.photo_urls),
                    photos_complete=details.photos_complete,
                    gallery_ms=round((perf_counter() - started) * 1000, 1),
                    publication_evidence=details.publication_evidence,
                )
            except Exception as exc:
                row["error"] = type(exc).__name__
            report["regions"].append(row)
            print(json.dumps(row, ensure_ascii=True), flush=True)
    finally:
        await service.close()
        await asyncio.to_thread(
            DESTINATION.write_text,
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


if __name__ == "__main__":
    asyncio.run(main())
