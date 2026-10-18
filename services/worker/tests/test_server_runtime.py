import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from vpars_worker.runtime import BOT_ROOT, ROOT, WorkerSettings, wait_for_parser


def test_local_worker_defaults_remain_local(monkeypatch):
    for name in ("WORKER_PARSER_URL", "WORKER_MANAGE_PARSER", "WORKER_STATE_DIR", "SOURCE_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    settings = WorkerSettings(_env_file=None)
    assert str(settings.worker_parser_url).rstrip("/") == "http://127.0.0.1:8002"
    assert settings.worker_manage_parser is True
    assert settings.worker_state_dir == ROOT / ".artifacts"
    assert settings.source_config == BOT_ROOT / "config/sources.json"


def test_container_worker_reads_external_parser_and_persistent_state(monkeypatch, tmp_path):
    monkeypatch.setenv("WORKER_PARSER_URL", "http://parser:8002")
    monkeypatch.setenv("WORKER_MANAGE_PARSER", "false")
    monkeypatch.setenv("WORKER_STATE_DIR", str(tmp_path))
    settings = WorkerSettings(_env_file=None)
    assert str(settings.worker_parser_url).rstrip("/") == "http://parser:8002"
    assert settings.worker_manage_parser is False
    assert settings.worker_state_dir == tmp_path


def test_shared_local_env_source_path_keeps_bot_directory(monkeypatch):
    monkeypatch.setenv("SOURCE_CONFIG", "config/sources.json")
    assert WorkerSettings(_env_file=None).source_config == BOT_ROOT / "config/sources.json"


@pytest.mark.parametrize("suffix", ["", "/"])
async def test_parser_readiness_uses_configured_address(suffix):
    requests = []

    async def ready(request):
        requests.append(request.path)
        return web.json_response({"status": "ready"})

    app = web.Application()
    app.router.add_get("/health/ready", ready)
    async with TestServer(app) as server:
        await wait_for_parser(str(server.make_url("")).rstrip("/") + suffix)
    assert requests == ["/health/ready"]
