"""Early-listening FC gateway. Model initialization belongs to /initialize.

FC HTTP trigger MUST enforce authentication. Synchronous inference and NAS-backed
asynchronous jobs share one bounded GPU slot per instance. FC owns async delivery.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import logging
import json
import os
import secrets
from pathlib import Path
import sys

import httpx
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import Response, FileResponse
from starlette.formparsers import MultiPartException
from pydantic import BaseModel
from oxr.server.schemas import SyncParseResponse, AsyncSubmitResponse, AsyncQueryResponse


class BusinessError(BaseModel):
    code: int
    message: str
    warnings: list[str] = []
    data: None = None

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("fc_allinone", ROOT / "docker/allinone_entrypoint.py")
supervisor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)
JOB_SPEC = importlib.util.spec_from_file_location("fc_jobs", ROOT / "deploy/fc/jobs.py")
jobs = importlib.util.module_from_spec(JOB_SPEC)
JOB_SPEC.loader.exec_module(jobs)
LOG = logging.getLogger("oxr.fc")


class Runtime:
    def __init__(self):
        self.config = supervisor.AllInOneConfig.from_env()
        if self.config.api_host != "127.0.0.1" or self.config.api_port == 9000:
            raise ValueError("FC backend must listen on loopback, outside gateway port 9000")
        self.task = None
        self.stop_event = asyncio.Event()
        self.init_lock = asyncio.Lock()
        self.parse_lock = asyncio.Lock()
        self.jobs = jobs.JobStore()
        self.ready = False
        self.stopping = False
        self.client = httpx.AsyncClient(trust_env=False, timeout=httpx.Timeout(600, connect=3))
        self.body_limit = int(os.getenv("OXR_FC_MAX_BODY_BYTES", str(20 * 1024 * 1024)))
        if not 1 <= self.body_limit <= 24 * 1024 * 1024:
            raise ValueError("FC request body limit must be between 1 byte and 24 MiB")

    def healthy(self):
        return self.ready and not self.stopping and self.task is not None and not self.task.done()

    async def initialize(self):
        async with self.init_lock:
            if self.stopping:
                raise RuntimeError("runtime stopping")
            if self.healthy():
                return
            if self.task is not None:
                raise RuntimeError("initialization previously failed; replace instance")
            self.task = asyncio.create_task(supervisor._run(self.config, self.stop_event))
            try:
                async with asyncio.timeout(self.config.startup_timeout):
                    while not self.task.done() and not self.stopping:
                        if await supervisor._healthcheck(self.config) == 0:
                            if self.task.done() or self.stopping:
                                break
                            self.ready = True
                            return
                        await asyncio.sleep(1)
                raise RuntimeError("model services exited or stopped during initialization")
            except BaseException:
                self.ready = False
                await self.stop_models()
                raise

    async def stop_models(self):
        self.stopping = True
        self.ready = False
        self.stop_event.set()
        if self.task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self.task), self.config.shutdown_timeout + 10)
            except asyncio.TimeoutError:
                self.task.cancel()
                await asyncio.gather(self.task, return_exceptions=True)

    async def close(self):
        await self.stop_models()
        await self.client.aclose()


def create_app(runtime_factory=Runtime):
    @asynccontextmanager
    async def lifespan(app):
        runtime = runtime_factory()
        app.state.runtime = runtime
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(title="OXR on Alibaba FC", version="1.1.0", lifespan=lifespan)

    @app.get("/")
    async def root():
        return {"service": "oxr-fc", "version": "1.1.0", "parse_path": "/v1/parse/sync"}

    @app.get("/health/live")
    async def live():
        return {"status": "alive"}

    @app.get("/health/ready")
    async def ready(request: Request):
        runtime = request.app.state.runtime
        if not runtime.healthy():
            raise HTTPException(503, "models_not_ready")
        if await supervisor._healthcheck(runtime.config) != 0:
            runtime.ready = False
            raise HTTPException(503, "model_health_failed")
        return {"status": "ready"}

    @app.post("/initialize", include_in_schema=False)
    async def initialize(request: Request):
        try:
            await request.app.state.runtime.initialize()
        except Exception:
            LOG.exception("FC initialization failed")
            raise HTTPException(500, "initialization_failed; inspect FC instance logs")
        return {"status": "ready"}

    upload_schema = {"requestBody": {"required": True, "content": {"multipart/form-data": {"schema": {
        "type": "object", "required": ["file"], "properties": {
            "file": {"type": "string", "format": "binary", "description": "One PDF or single-frame document image; total body <=20 MiB, PDF <=20 pages."},
            "output_formats": {"type": "array", "items": {"type": "string", "enum": ["markdown", "json"]}, "default": ["markdown", "json"]},
            "feature_config": {"type": "string", "description": 'JSON string, e.g. {"keep_header_footer":true}.'},
            "extra": {"type": "string", "description": "JSON string: temperature (0..2), top_p (0..1), repetition_penalty (>0)."},
            "element_formats": {"type": "string", "description": 'JSON string; image=base64 (default) or none, formula=latex, table=html/markdown, cs=smiles/image.'},
            "api_key": {"type": "string", "description": "Optional legacy field; not authentication. FC Bearer header is required."}}}}}}}

    import copy
    demo_schema = copy.deepcopy(upload_schema)
    demo_schema["requestBody"]["content"]["multipart/form-data"]["schema"]["properties"] = {
        "file": {"type":"string","format":"binary"},
        "temperature": {"type":"number","minimum":0,"maximum":2,"default":0},
        "top_p": {"type":"number","minimum":0,"maximum":1,"default":1},
        "repetition_penalty": {"type":"number","minimum":0.1,"maximum":2,"default":1},
        "keep_header_footer": {"type":"boolean","default":True},
    }

    @app.post("/v1/demo/jobs", openapi_extra=demo_schema, summary="Submit a persistent Demo job with page previews and image resources")
    @app.post("/v1/parse/async", openapi_extra=upload_schema, response_model=AsyncSubmitResponse | BusinessError,
              summary="Persist document and dispatch a durable FC asynchronous worker invocation")
    @app.post("/v1/parse/sync", openapi_extra=upload_schema, response_model=SyncParseResponse | BusinessError,
              summary="Parse PDF/image and return Markdown and/or structured JSON")
    async def parse(request: Request):
        runtime = request.app.state.runtime
        if not runtime.healthy():
            raise HTTPException(503, "models_not_ready")
        content_type = request.headers.get("content-type", "")
        if not content_type.lower().startswith("multipart/form-data;"):
            raise HTTPException(415, "multipart/form-data required")
        if "boundary=" not in content_type.lower():
            raise HTTPException(400, "multipart boundary required")
        length = request.headers.get("content-length")
        if length is not None:
            try:
                if not 0 < int(length) <= runtime.body_limit:
                    raise HTTPException(413, "request_too_large")
            except ValueError:
                raise HTTPException(400, "invalid_content_length")
        # No await between locked() and acquire(): one event loop, one request slot.
        if runtime.parse_lock.locked():
            raise HTTPException(429, "instance_busy")
        async with runtime.parse_lock:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > runtime.body_limit:
                    raise HTTPException(413, "request_too_large")
                body.extend(chunk)
            # Default to inline extracted images: local filesystem URLs are not durable.
            # Reject local URL outputs and unsafe document shapes before GPU processing.
            from starlette.formparsers import MultiPartParser
            async def body_stream():
                yield bytes(body)
            try:
                form = await MultiPartParser(request.headers, body_stream(), max_files=1, max_fields=20).parse()
            except MultiPartException:
                raise HTTPException(400, "invalid multipart body")
            try:
                await asyncio.to_thread(validate_document, form, runtime)
                fields = [(k, v) for k, v in form.multi_items() if isinstance(v, str)]
                if not any(k == "api_key" for k, _ in fields):
                    fields.append(("api_key", "dummy"))
                if not any(k == "element_formats" for k, _ in fields):
                    fields.append(("element_formats", '{"image":"base64"}'))
                upload = form.get("file")
                data = await upload.read()
                if request.url.path in {"/v1/parse/async", "/v1/demo/jobs"}:
                    is_demo = request.url.path == "/v1/demo/jobs"
                    if is_demo:
                        values = dict(fields)
                        # Preserve the original Demo form while converting to
                        # the same validated options used by core parsing.
                        extra = {k:values[k] for k in ("temperature","top_p","repetition_penalty") if k in values}
                        import math
                        for key,minimum,maximum in (("temperature",0,2),("top_p",0,1),("repetition_penalty",0.1,2)):
                            try:
                                value = float(extra.get(key, 0 if key=="temperature" else 1))
                                if not math.isfinite(value) or not minimum <= value <= maximum:
                                    raise ValueError()
                            except (TypeError, ValueError):
                                raise HTTPException(422, f"{key} must be finite and between {minimum} and {maximum}")
                            extra[key] = value
                        keep = values.get("keep_header_footer","true").lower()
                        if keep not in {"true","1","on","yes","false","0","off","no"}:
                            raise HTTPException(422,"keep_header_footer must be boolean")
                        feature = {"keep_header_footer": keep in {"true","1","on","yes"}}
                        fields = [(k,v) for k,v in fields if k not in {"extra","feature_config"}]
                        fields.extend([("extra",json.dumps(extra)),("feature_config",json.dumps(feature))])
                    # Validate core options before storing or invoking a worker.
                    from oxr.server.routes import _parse_request_options
                    values = dict(fields)
                    _, errors = _parse_request_options(
                        [v for k,v in fields if k == "output_formats"],
                        values.get("feature_config"), values.get("extra"), values.get("element_formats"))
                    if errors:
                        return {"code": 1133, "message": "; ".join(errors), "warnings": [], "data": None}
                    job = await asyncio.to_thread(runtime.jobs.create,
                        Path(upload.filename).name, data, fields, upload.content_type, "demo" if is_demo else "parse")
                    try:
                        await jobs.dispatch(job, runtime.client)
                    except (RuntimeError, httpx.HTTPError):
                        # Transport failure may have an unknown acceptance outcome.
                        # Do not retry/replay automatically or erase the journal.
                        raise HTTPException(503, {"error": "async_dispatch_not_confirmed", "task_id": job["task_id"]})
                    if is_demo:
                        return {"code":0,"message":"success","data":{"id":job["task_id"],"status":"queued"}}
                    return {"code": 0, "message": "Task submitted", "warnings": [],
                            "data": {"task_id": job["task_id"], "status": "queuing"}}
                parts = [(k, (None, v)) for k, v in fields]
                parts.append(("file", (Path(upload.filename or "document").name, data, upload.content_type)))
                del body
                result = await runtime.client.post(
                    runtime.config.api_url + "/v1/parse/sync", files=parts,
                )
                if len(result.content) > 28 * 1024 * 1024:
                    raise HTTPException(413,"result_too_large; use image=none or a smaller document")
                return Response(content=result.content, status_code=result.status_code,
                                media_type="application/json")
            except httpx.TimeoutException:
                # The model backend may still be executing. Do not admit a new document.
                await runtime.stop_models()
                raise HTTPException(504, "parse_timeout; instance requires replacement")
            except httpx.HTTPError:
                await runtime.stop_models()
                raise HTTPException(502, "backend_unavailable")
            except asyncio.CancelledError:
                await asyncio.shield(runtime.stop_models())
                raise
            finally:
                await form.close()

    @app.post("/v1/parse/async_check", response_model=AsyncQueryResponse,
              summary="Read persisted async status/result from shared NAS")
    async def async_check(request: Request, task_id: str = Form(...), api_key: str | None = Form(None)):
        try:
            job = await asyncio.to_thread(request.app.state.runtime.jobs.get, task_id)
        except KeyError:
            raise HTTPException(404, "task_not_found_or_expired")
        return {"code": 0, "message": "Task status", "warnings": [],
                "data": request.app.state.runtime.jobs.public(job)}

    @app.post("/internal/tasks/run", include_in_schema=False)
    async def run_job(request: Request):
        # FC trigger authenticates the request; per-job ticket additionally
        # prevents ordinary callers from manufacturing worker invocations.
        try:
            body = await request.json()
            if not isinstance(body, dict) or set(body) != {"task_id", "ticket"}:
                raise ValueError()
            runtime = request.app.state.runtime
            job = await asyncio.to_thread(runtime.jobs.get, body["task_id"])
            if not isinstance(body["ticket"], str) or not secrets.compare_digest(job["ticket"], body["ticket"]):
                raise ValueError()
        except (ValueError, KeyError):
            raise HTTPException(403, "invalid_job_ticket")
        if not runtime.healthy():
            raise HTTPException(503, "models_not_ready")
        async with runtime.parse_lock:
            if not await asyncio.to_thread(runtime.jobs.claim, job):
                return {"status": "duplicate_or_terminal"}
            try:
                if job.get("kind") == "demo":
                    response=await runtime.client.post(runtime.config.api_url+"/v1/internal/fc/demo",
                                                       json={"task_id":job["task_id"],"ticket":job["ticket"]})
                else:
                    fields = [(k,(None,v)) for k,v in job["fields"]]
                    fields.append(("api_key", (None, "dummy")))
                    data = await asyncio.to_thread((runtime.jobs.directory(job["task_id"])/"input.bin").read_bytes)
                    fields.append(("file",(job["file_name"],data,job["content_type"])))
                    response = await runtime.client.post(runtime.config.api_url+"/v1/parse/sync", files=fields)
                if response.status_code != 200:
                    raise RuntimeError("Backend rejected the worker request")
                if len(response.content) > 28 * 1024 * 1024:
                    await asyncio.to_thread(runtime.jobs.finish, job, {"code":1137,
                        "message":"Result exceeds FC response budget; use image=none or a smaller document","data":None})
                else:
                    await asyncio.to_thread(runtime.jobs.finish, job, response.json())
            except asyncio.CancelledError:
                # Journal remains processing; poll exposes an explicit failure
                # after its bounded execution deadline if FC terminates us.
                await asyncio.shield(runtime.stop_models())
                raise
            except Exception:
                LOG.exception("Async document worker failed")
                await asyncio.to_thread(runtime.jobs.fail, job, "Document worker failed; inspect FC logs")
                await runtime.stop_models()
                raise HTTPException(502, "worker_failed")
        return {"status": "completed"}

    def demo_job(runtime, job_id):
        try:
            job=runtime.jobs.get(job_id)
            if job.get("kind")!="demo":raise KeyError(job_id)
        except KeyError:
            raise HTTPException(404,"demo_job_not_found_or_expired")
        return job

    @app.get("/v1/demo/jobs/{job_id}")
    async def demo_status(request: Request,job_id:str):
        runtime=request.app.state.runtime
        job=await asyncio.to_thread(demo_job,runtime,job_id)
        manifest_path=runtime.jobs.directory(job_id)/"manifest.json"
        manifest={"id":job_id,"file_name":job["file_name"],"pages":[],"result_url":None,"metadata":{},"error":job.get("error")}
        if manifest_path.exists():
            manifest.update(json.loads(await asyncio.to_thread(manifest_path.read_text,encoding="utf-8")))
        manifest["status"]="queued" if job["status"]=="queuing" else job["status"]
        manifest["error"]=job.get("error")
        return {"code":0,"message":"success","data":{k:v for k,v in manifest.items() if k not in {"assets","expires_at"}}}

    @app.get("/v1/demo/jobs/{job_id}/result")
    async def demo_result(request:Request,job_id:str):
        job=await asyncio.to_thread(demo_job,request.app.state.runtime,job_id)
        if job["status"]!="success":raise HTTPException(409,"document_not_ready")
        return {"code":0,"message":"success","data":job["result"]}

    @app.get("/v1/demo/jobs/{job_id}/assets/{asset_id}")
    async def demo_asset(request:Request,job_id:str,asset_id:str):
        import re
        runtime=request.app.state.runtime
        await asyncio.to_thread(demo_job,runtime,job_id)
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*\.(?:png|jpg|jpeg|webp)",asset_id):
            raise HTTPException(404,"asset_not_found")
        directory=runtime.jobs.directory(job_id)
        try:
            manifest=json.loads(await asyncio.to_thread((directory/"manifest.json").read_text,encoding="utf-8"))
        except FileNotFoundError:raise HTTPException(404,"asset_not_found")
        path=directory/"images"/asset_id
        if asset_id not in manifest.get("assets",[]) or path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            raise HTTPException(404,"asset_not_found")
        return FileResponse(path,headers={"Cache-Control":"private, no-store"})

    original_openapi = app.openapi
    def authenticated_openapi():
        schema = original_openapi()
        schema.setdefault("components", {}).setdefault("securitySchemes", {})["FCBearer"] = {"type": "http", "scheme": "bearer"}
        schema["security"] = [{"FCBearer": []}]
        return schema
    app.openapi = authenticated_openapi

    return app


def validate_document(form, runtime):
    import json
    from starlette.datastructures import UploadFile
    from PIL import Image
    upload = form.get("file")
    if not isinstance(upload, UploadFile) or not upload.filename:
        raise HTTPException(422, "file required")
    if sum(k == "file" for k, _ in form.multi_items()) != 1:
        raise HTTPException(422, "exactly one file required")
    try:
        formats = json.loads(form.get("element_formats", '{"image":"base64"}'))
        if not isinstance(formats, dict) or formats.get("image", "url") not in {"base64", "none"}:
            raise HTTPException(422, "FC requires image=base64 or none; local asset URLs are unsupported")
    except (ValueError, TypeError):
        raise HTTPException(422, "invalid element_formats JSON")
    suffix = Path(upload.filename).suffix.lower()
    try:
        if suffix == ".pdf":
            import sopdf
            with sopdf.open(stream=upload.file.read()) as doc:
                count = len(doc.pages)
                if not 1 <= count <= int(os.getenv("OXR_FC_MAX_PAGES", "20")):
                    raise HTTPException(413, "PDF page limit exceeded")
        elif suffix in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}:
            with Image.open(upload.file) as image:
                if image.width * image.height > 25_000_000 or getattr(image, "n_frames", 1) > 1:
                    raise HTTPException(413, "image size/frame limit exceeded")
                image.verify()
        else:
            raise HTTPException(415, "supported: PDF or single-frame document image")
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(422, "invalid_document")
    finally:
        upload.file.seek(0)


app = create_app()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000, workers=1, timeout_keep_alive=900)
