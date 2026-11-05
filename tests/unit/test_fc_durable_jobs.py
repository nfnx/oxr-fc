"""Persistent journal/duplicate dispatch contract; no GPU inference claim."""
import asyncio
import importlib.util
import json
from pathlib import Path
import sys
import time

import httpx
import pytest

PATH = Path(__file__).parents[2] / "deploy/fc/jobs.py"
SPEC = importlib.util.spec_from_file_location("fc_durable_jobs_test", PATH)
jobs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(jobs)


def test_reopen_on_another_worker_preserves_result(tmp_path):
    store = jobs.JobStore(tmp_path)
    job = store.create("file.pdf", b"pdf", [("api_key", "not-persisted"), ("output_formats", "json")], "application/pdf")
    assert "not-persisted" not in (store.directory(job["task_id"])/"job.json").read_text()
    second = jobs.JobStore(tmp_path)
    assert second.claim(second.get(job["task_id"]))
    assert not store.claim(job)
    second.finish(job, {"code":0,"data":{"error":None,"result":{"file_name":"file.pdf","outputs":{"json":{}}},"metadata":{"page_num":1}}})
    result=store.get(job["task_id"])
    assert result["status"]=="success" and result["metadata"]["page_num"]==1
    assert not (store.directory(job["task_id"])/"input.bin").exists()
    public=store.public(result)
    assert "ticket" not in public and "fields" not in public


def test_claim_is_atomic_between_threads(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    store=jobs.JobStore(tmp_path); job=store.create("x.png",b"x",[],"image/png")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results=list(pool.map(lambda _:jobs.JobStore(tmp_path).claim(job),range(8)))
    assert sum(results)==1


def test_interrupted_worker_exposes_failure_without_replay(tmp_path):
    store=jobs.JobStore(tmp_path); job=store.create("x",b"x",[],"x")
    store.claim(job)
    record=store.get(job["task_id"]); record["started_at"]=time.time()-store.deadline-1
    store.write(store.directory(job["task_id"])/"job.json",record)
    assert store.get(job["task_id"])["status"]=="failed"
    assert not store.claim(job)


@pytest.mark.parametrize("value",["../x","x/../y","a"*31,"A"*32,"a"*33])
def test_task_id_cannot_escape_store(tmp_path,value):
    with pytest.raises(KeyError): jobs.JobStore(tmp_path).get(value)


def test_business_error_persists_as_failed(tmp_path):
    store=jobs.JobStore(tmp_path); job=store.create("x",b"x",[],"x"); store.claim(job)
    store.finish(job,{"code":1137,"message":"parse failed","data":None})
    assert store.get(job["task_id"])["status"]=="failed"


def test_dispatch_uses_native_async_and_small_reference(monkeypatch):
    monkeypatch.setenv("OXR_FC_ASYNC_BASE_URL","https://example.fcapp.run")
    monkeypatch.setenv("OXR_FC_ASYNC_TOKEN","test-token")
    captured=[]
    async def handler(request):
        captured.append(request)
        return httpx.Response(202,headers={"X-Fc-Request-Id":"test-invocation"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await jobs.dispatch({"task_id":"a"*32,"ticket":"ticket"},client)
    assert asyncio.run(run())=="test-invocation"
    assert captured[0].headers["X-Fc-Invocation-Type"]=="Async"
    assert json.loads(captured[0].content)=={"task_id":"a"*32,"ticket":"ticket"}


def test_dispatch_does_not_follow_redirect_or_retry(monkeypatch):
    monkeypatch.setenv("OXR_FC_ASYNC_BASE_URL","https://example.fcapp.run")
    monkeypatch.setenv("OXR_FC_ASYNC_TOKEN","test-token")
    calls=[]
    async def handler(request):
        calls.append(request); return httpx.Response(302,headers={"Location":"https://other.example"})
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await jobs.dispatch({"task_id":"a"*32,"ticket":"ticket"},client)
    with pytest.raises(RuntimeError):asyncio.run(run())
    assert len(calls)==1
