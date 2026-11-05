"""Early-listening FC gateway. Model initialization belongs to /initialize.

FC HTTP trigger MUST enforce authentication. Only synchronous parsing is exposed;
the original process-local async jobs and local asset URLs are not distributed storage.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib.util
import logging
import os
from pathlib import Path
import sys

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.formparsers import MultiPartException

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("fc_allinone", ROOT / "docker/allinone_entrypoint.py")
supervisor = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = supervisor
SPEC.loader.exec_module(supervisor)
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

    app = FastAPI(title="OXR on Alibaba FC", version="1.0.0", lifespan=lifespan)

    @app.get("/")
    async def root():
        return {"service": "oxr-fc", "version": "1.0.0", "parse_path": "/v1/parse/sync"}

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

    @app.post("/v1/parse/sync")
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
                parts = [(k, (None, v)) for k, v in fields]
                parts.append(("file", (Path(upload.filename or "document").name, data, upload.content_type)))
                del body
                result = await runtime.client.post(
                    runtime.config.api_url + "/v1/parse/sync", files=parts,
                )
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
