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


def test_durable_async_requires_ready_and_query_requires_id(client):
    assert client.post("/v1/parse/async").status_code == 503
    assert client.post("/v1/parse/async_check").status_code == 422
    assert client.get("/demo").status_code == 404


def test_async_submit_execute_poll_and_duplicate_are_persistent(client, tmp_path, monkeypatch):
    monkeypatch.setenv("OXR_FC_ASYNC_BASE_URL", "https://worker.example")
    monkeypatch.setenv("OXR_FC_ASYNC_TOKEN", "test-only")
    runtime=client.app.state.runtime
    runtime.jobs=gateway.jobs.JobStore(tmp_path)
    executions=[]
    async def backend(request):
        if request.url.host=="worker.example":
            assert request.headers["X-Fc-Invocation-Type"]=="Async"
            return httpx.Response(202)
        executions.append(request)
        return httpx.Response(200,json={"code":0,"data":{"error":None,"result":{"file_name":"doc.png","outputs":{"markdown":"test"}},"metadata":{"page_num":1}}})
    runtime.client._transport=httpx.MockTransport(backend)
    client.post("/initialize")
    response=client.post("/v1/parse/async",files={"file":("doc.png",png(),"image/png")})
    assert response.status_code==200,response.text
    task_id=response.json()["data"]["task_id"]
    assert client.post("/v1/parse/async_check",data={"task_id":task_id}).json()["data"]["status"]=="queuing"
    job=runtime.jobs.get(task_id)
    ticket={"task_id":task_id,"ticket":job["ticket"]}
    assert client.post("/internal/tasks/run",json=ticket).status_code==200
    # Reopening the journal models a query hitting a different instance.
    runtime.jobs=gateway.jobs.JobStore(tmp_path)
    status=client.post("/v1/parse/async_check",data={"task_id":task_id}).json()
    assert status["data"]["status"]=="success"
    assert status["data"]["result"]["outputs"]["markdown"]=="test"
    assert client.post("/internal/tasks/run",json=ticket).json()["status"]=="duplicate_or_terminal"
    assert len(executions)==1


def test_worker_requires_private_job_ticket(client,tmp_path):
    client.app.state.runtime.jobs=gateway.jobs.JobStore(tmp_path)
    client.post("/initialize")
    assert client.post("/internal/tasks/run",json={"task_id":"a"*32,"ticket":"wrong"}).status_code==403


def test_openapi_describes_file_upload_and_bearer(client):
    schema=client.get("/openapi.json").json()
    assert schema["security"]==[{"FCBearer":[]}]
    body=schema["paths"]["/v1/parse/sync"]["post"]["requestBody"]
    assert body["content"]["multipart/form-data"]["schema"]["properties"]["file"]["format"]=="binary"


def test_demo_job_result_and_assets_survive_reopening_store(client,tmp_path,monkeypatch):
    import json
    monkeypatch.setenv("OXR_FC_ASYNC_BASE_URL","https://worker.example")
    monkeypatch.setenv("OXR_FC_ASYNC_TOKEN","test-only")
    runtime=client.app.state.runtime
    runtime.jobs=gateway.jobs.JobStore(tmp_path)
    async def backend(request):
        if request.url.host=="worker.example":return httpx.Response(202)
        assert request.url.path=="/v1/internal/fc/demo"
        body=json.loads(request.content); directory=runtime.jobs.directory(body["task_id"])
        (directory/"images").mkdir()
        (directory/"images/page-0000.png").write_bytes(png())
        runtime.jobs.write(directory/"manifest.json",{"id":body["task_id"],"pages":[{"page_num":0}],
            "assets":["page-0000.png"],"result_url":"/v1/demo/jobs/"+body["task_id"]+"/result"})
        return httpx.Response(200,json={"code":0,"data":{"error":None,"result":{"outputs":{"markdown":"demo"}},"metadata":{"page_num":1}}})
    runtime.client._transport=httpx.MockTransport(backend)
    client.post("/initialize")
    response=client.post("/v1/demo/jobs",files={"file":("doc.png",png(),"image/png")},data={"temperature":"0","keep_header_footer":"true"})
    assert response.status_code==200,response.text
    job_id=response.json()["data"]["id"]
    assert client.get(f"/v1/demo/jobs/{job_id}/result").status_code==409
    job=runtime.jobs.get(job_id)
    assert client.post("/internal/tasks/run",json={"task_id":job_id,"ticket":job["ticket"]}).status_code==200
    runtime.jobs=gateway.jobs.JobStore(tmp_path)
    assert client.get(f"/v1/demo/jobs/{job_id}").json()["data"]["status"]=="success"
    assert client.get(f"/v1/demo/jobs/{job_id}/result").json()["data"]["outputs"]["markdown"]=="demo"
    assert client.get(f"/v1/demo/jobs/{job_id}/assets/page-0000.png").content==png()
    assert client.get(f"/v1/demo/jobs/{job_id}/assets/not-present.png").status_code==404


def test_unknown_demo_job_is_not_a_local_file_read(client,tmp_path):
    client.app.state.runtime.jobs=gateway.jobs.JobStore(tmp_path)
    assert client.get("/v1/demo/jobs/"+"a"*32).status_code==404


@pytest.mark.parametrize("field,value", [("temperature","nan"),("top_p","inf"),("repetition_penalty","3")])
def test_demo_rejects_nonfinite_or_out_of_contract_options(client,field,value):
    client.post("/initialize")
    result=client.post("/v1/demo/jobs",files={"file":("x.png",png())},data={field:value})
    assert result.status_code==422
    assert not client.app.state.runtime.calls


def test_nonfinite_core_options_are_rejected():
    from oxr.server.routes import _parse_request_options
    for value in ("nan","inf","-inf"):
        _,errors=_parse_request_options(None,None,'{"temperature":"'+value+'"}',None)
        assert errors and "finite" in errors[0]


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
