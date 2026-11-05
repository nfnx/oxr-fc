"""Loopback-only awaited Demo execution for the FC-native async worker."""
import json
from pathlib import Path
import secrets
import time

from fastapi import APIRouter, HTTPException, Request

from deploy.fc.jobs import JobStore
from oxr.server.demo_store import DemoStore
from oxr.server.demo_api import _process_job

router=APIRouter()


@router.post("/internal/fc/demo", include_in_schema=False)
async def execute_demo(request: Request):
    try:
        payload=await request.json()
        journal=JobStore()
        job=journal.get(payload["task_id"])
        if job.get("kind")!="demo" or not secrets.compare_digest(job["ticket"],payload["ticket"]):
            raise ValueError()
    except (KeyError, ValueError, TypeError):
        raise HTTPException(403,"invalid_job_ticket")
    directory=journal.directory(job["task_id"])
    (directory/"images").mkdir(exist_ok=True)
    (directory/"lease").touch()
    store=DemoStore(root=journal.root,ttl=journal.ttl,max_bytes=10*1024**3)
    now=time.time()
    store._write_json(directory/"manifest.json",{
        "id":job["task_id"],"status":"queued","file_name":job["file_name"],
        "pages":[],"metadata":{},"error":None,"result_url":None,"assets":[],"expires_at":now+journal.ttl})
    fields=dict(job["fields"])
    extra={key:float(value) for key,value in json.loads(fields.get("extra","{}")).items()}
    feature=json.loads(fields.get("feature_config","{}"))
    # Await the entire inference within the FC worker HTTP request. Do not use
    # FastAPI BackgroundTasks: a returned invocation may be frozen/reclaimed.
    await _process_job(store,job["task_id"],(directory/"input.bin").read_bytes(),job["file_name"],
                       extra,feature.get("keep_header_footer",True))
    manifest=store.get(job["task_id"])
    if manifest["status"]!="success":
        return {"code":1137,"message":manifest.get("error") or "Demo processing failed","data":None}
    result=store.result(job["task_id"])
    return {"code":0,"data":{"error":None,"result":result,"metadata":result.get("metadata",{})}}
