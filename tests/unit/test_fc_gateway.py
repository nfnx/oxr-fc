"""Gateway lifecycle and wire contract checks; model backend is simulated, not GPU evidence."""
import asyncio
import importlib.util
import io
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi.testclient import TestClient
from PIL import Image
import pytest

PATH = Path(__file__).parents[2] / "deploy/fc/gateway.py"
SPEC = importlib.util.spec_from_file_location("fc_gateway_test", PATH)
gateway = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gateway)


def png():
    out = io.BytesIO()
    Image.new("RGB", (8, 8), "white").save(out, format="PNG")
    return out.getvalue()


class FakeRuntime:
    def __init__(self):
        self.ready = False
        self.stopping = False
        self.body_limit = 20000
        self.config = SimpleNamespace(api_url="http://model.local")
        self.parse_lock = asyncio.Lock()
        self.calls = []
        async def backend(request):
            self.calls.append(await request.aread())
            return httpx.Response(200, json={"code": 0, "data": {"result": {"outputs": []}}})
        self.client = httpx.AsyncClient(transport=httpx.MockTransport(backend))

    def healthy(self):
        return self.ready and not self.stopping

    async def initialize(self):
        self.ready = True

    async def stop_models(self):
        self.stopping = True

    async def close(self):
        await self.client.aclose()


@pytest.fixture
def client():
    with TestClient(gateway.create_app(FakeRuntime)) as c:
        yield c


def test_early_http_does_not_initialize_models(client):
    assert client.get("/health/live").status_code == 200
    assert client.get("/health/ready").status_code == 503
    assert client.post("/v1/parse/sync").status_code == 503
    assert not client.app.state.runtime.ready


def test_initializer_failure_is_not_ready(client, monkeypatch):
    async def fail():
        raise RuntimeError("synthetic startup failure")
    monkeypatch.setattr(client.app.state.runtime, "initialize", fail)
    assert client.post("/initialize").status_code == 500
    assert client.get("/health/ready").status_code == 503


def test_multipart_forwarding_and_inline_default(client):
    assert client.post("/initialize").status_code == 200
    response = client.post("/v1/parse/sync", files={"file": ("doc.png", png(), "image/png")},
                           data={"output_formats": "markdown"})
    assert response.status_code == 200 and response.json()["code"] == 0
    raw = client.app.state.runtime.calls[0]
    assert b'"image":"base64"' in raw and b"dummy" in raw and b"markdown" in raw


@pytest.mark.parametrize("formats", ['{"image":"url"}', '{"table":"html"}', '[]', 'broken'])
def test_reject_instance_local_image_urls(client, formats):
    client.post("/initialize")
    r = client.post("/v1/parse/sync", files={"file": ("doc.png", png())},
                    data={"element_formats": formats})
    assert r.status_code == 422
    assert not client.app.state.runtime.calls


def test_busy_rejected_before_backend(client):
    client.post("/initialize")
    lock = client.app.state.runtime.parse_lock
    client.portal.call(lock.acquire)
    try:
        response = client.post("/v1/parse/sync", files={"file": ("doc.png", png())})
        assert response.status_code == 429
        assert not client.app.state.runtime.calls
    finally:
        client.portal.call(lock.release)


def test_unsupported_and_invalid_documents(client):
    client.post("/initialize")
    for filename, data, status in [("doc.exe", b"test", 415), ("doc.png", b"invalid", 422)]:
        assert client.post("/v1/parse/sync", files={"file": (filename, data)}).status_code == status


def test_request_size_limit(client):
    client.post("/initialize")
    client.app.state.runtime.body_limit = 10
    assert client.post("/v1/parse/sync", files={"file": ("doc.png", png())}).status_code == 413


def test_process_local_async_and_demo_are_not_exposed(client):
    for method, path in [("post", "/v1/parse/async"), ("post", "/v1/parse/async_check"), ("get", "/demo")]:
        assert getattr(client, method)(path).status_code == 404


def test_backend_timeout_fails_closed(client):
    client.post("/initialize")
    async def timeout(request):
        raise httpx.ReadTimeout("synthetic timeout")
    client.app.state.runtime.client._transport = httpx.MockTransport(timeout)
    r = client.post("/v1/parse/sync", files={"file": ("doc.png", png())})
    assert r.status_code == 504
    assert client.post("/v1/parse/sync", files={"file": ("doc.png", png())}).status_code == 503


def test_max_sequences_config_does_not_change_legacy_default():
    cls = gateway.supervisor.AllInOneConfig
    assert cls.from_env({"MODEL_PATH": "/models"}).max_num_seqs == 16
    config = cls.from_env({"MODEL_PATH": "/models", "VLLM_MAX_NUM_SEQS": "4"})
    command = config.vllm_command()
    assert command[command.index("--max-num-seqs") + 1] == "4"
    with pytest.raises(ValueError):
        cls.from_env({"MODEL_PATH": "/models", "VLLM_MAX_NUM_SEQS": "0"})


def test_real_runtime_initialization_is_serialized_and_idempotent(monkeypatch):
    async def check():
        monkeypatch.setenv("MODEL_PATH", "/models/test")
        monkeypatch.setenv("OXR_API_HOST", "127.0.0.1")
        calls = []
        async def run(config, stop):
            calls.append(config)
            await stop.wait()
            return 0
        async def healthy(config):
            return 0
        monkeypatch.setattr(gateway.supervisor, "_run", run)
        monkeypatch.setattr(gateway.supervisor, "_healthcheck", healthy)
        runtime = gateway.Runtime()
        try:
            await asyncio.gather(runtime.initialize(), runtime.initialize())
            await asyncio.sleep(0)
            assert runtime.healthy() and len(calls) == 1
            await runtime.stop_models()
            assert not runtime.healthy() and runtime.task.done()
        finally:
            await runtime.close()
    asyncio.run(check())


def test_real_runtime_start_failure_cannot_be_reported_ready(monkeypatch):
    async def check():
        monkeypatch.setenv("MODEL_PATH", "/models/test")
        monkeypatch.setenv("OXR_API_HOST", "127.0.0.1")
        async def fail(config, stop):
            return 1
        async def unhealthy(config):
            await asyncio.sleep(0)
            return 1
        monkeypatch.setattr(gateway.supervisor, "_run", fail)
        monkeypatch.setattr(gateway.supervisor, "_healthcheck", unhealthy)
        runtime = gateway.Runtime()
        try:
            with pytest.raises(RuntimeError):
                await runtime.initialize()
            assert not runtime.healthy()
        finally:
            await runtime.close()
    asyncio.run(check())
