"""One sequential live request per enabled region through the existing gRPC gateway."""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from vpars_parser.errors import ParserError  # noqa: E402
from vpars_parser.gateway import GrpcGatewayClient  # noqa: E402
from vpars_parser.models import ParseRequest  # noqa: E402
from vpars_parser.service import ParserService  # noqa: E402
from vpars_parser.settings import Settings  # noqa: E402


async def run(target: str):
    settings = Settings.from_env().model_copy(update={"gateway_target": target})
    gateway = GrpcGatewayClient(settings)
    service = ParserService(gateway, settings)
    report = {
        "checked_at": datetime.now(UTC).isoformat(),
        "kind": "live_gateway_smoke",
        "results": [],
        "live_vinted_verified": False,
    }
    try:
        await gateway.list_regions("smoke-control")
        for region in ("UK", "FR", "IT", "DE"):
            try:
                result = await service.parse(
                    ParseRequest(region_id=region, request_id=f"smoke-{region}")
                )
                report["results"].append(
                    {
                        "region": region,
                        "status": result.completeness,
                        "items": len(result.items),
                        "cards_skipped": result.cards_skipped,
                        "pagination": result.pagination.model_dump(),
                        "timings": result.timings.model_dump(),
                    }
                )
            except ParserError as exc:
                report["results"].append({"region": region, "error": exc.detail.model_dump()})
        report["live_vinted_verified"] = all(
            row.get("status") == "complete" for row in report["results"]
        )
    except ParserError as exc:
        report["control_error"] = exc.detail.model_dump()
    finally:
        await service.close()
    folder = ROOT / ".artifacts"
    folder.mkdir(exist_ok=True)
    output = folder / "live-smoke.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0 if report["live_vinted_verified"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", default=Settings.from_env().gateway_target)
    raise SystemExit(asyncio.run(run(parser.parse_args().target)))
