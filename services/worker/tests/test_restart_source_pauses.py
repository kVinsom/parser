import importlib.util
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

from vpars_bot.db import utcnow


async def test_restart_checkpoint_keeps_source_deadlines_not_maintenance_hold(
    tmp_path, monkeypatch
):
    source = Path(__file__).resolve().parents[1] / "tools/restart_safely.py"
    spec = importlib.util.spec_from_file_location("rate_restart_tool", source)
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    monkeypatch.setattr(tool, "ROOT", tmp_path)
    folder = tmp_path / ".artifacts"
    folder.mkdir()
    now = utcnow()
    future = lambda seconds: now + timedelta(seconds=seconds)  # noqa: E731
    checkpoint = folder / "source-cooldowns.json"
    checkpoint.write_text(
        json.dumps(
            {
                "version": 1,
                "regions": {
                    "FR": {"code": "SourceRateLimited", "until": future(900).isoformat()},
                },
            }
        ),
        encoding="utf-8",
    )
    responses = iter(
        [
            [
                {"region": "FR", "code": "SourceRateLimited", "until": future(60)},
                {"region": "GB", "code": "SourceRateLimited", "until": future(120)},
                {"region": "IT", "code": "SourceRateLimited", "until": future(-1)},
            ],
            [
                {"id": "detail", "region": "DE", "code": "AccessBlocked", "next_due": future(600)},
                {
                    "id": "expired",
                    "region": "IT",
                    "code": "SourceRateLimited",
                    "next_due": future(-1),
                },
            ],
        ]
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def execute(self, statement):
            values = next(responses)
            return SimpleNamespace(mappings=lambda: values)

    await tool.checkpoint_source_pauses(Session, {"deliveries": {"detail": future(30).isoformat()}})
    saved = json.loads(checkpoint.read_text(encoding="utf-8"))["regions"]
    assert saved == {
        "FR": {"code": "SourceRateLimited", "until": future(900).isoformat()},
        "UK": {"code": "SourceRateLimited", "until": future(120).isoformat()},
        "DE": {"code": "AccessBlocked", "until": future(30).isoformat()},
    }
