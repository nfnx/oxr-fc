"""NAS-backed job journal plus FC-native durable async dispatch.

No background inference is started after an HTTP submission returns. A separate
FC asynchronous invocation owns the entire worker request and its GPU lifetime.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import shutil
import time
from uuid import uuid4

import httpx


class JobStore:
    def __init__(self, root=None):
        self.root = Path(root or os.environ.get("OXR_JOB_ROOT", "/mnt/oxr-models/.oxr-jobs"))
        self.ttl = int(os.environ.get("OXR_JOB_RETENTION_SECONDS", "604800"))
        self.deadline = int(os.environ.get("OXR_JOB_EXECUTION_SECONDS", "720"))
        self.queue_ttl = int(os.environ.get("OXR_JOB_QUEUE_SECONDS", "86400"))

    def cleanup(self):
        """Remove only expired task directories owned by this journal namespace."""
        if not self.root.exists():
            return
        now = time.time()
        for path in self.root.iterdir():
            if path.is_symlink() or not re.fullmatch(r"[0-9a-f]{32}", path.name):
                continue
            try:
                body = json.loads((path / "job.json").read_text(encoding="utf-8"))
                if body.get("expires_at", float("inf")) < now:
                    # Direct child checked above; never follow arbitrary task paths.
                    shutil.rmtree(path)
            except (FileNotFoundError, ValueError):
                continue

    def directory(self, task_id):
        if not isinstance(task_id, str) or not re.fullmatch(r"[0-9a-f]{32}", task_id):
            raise KeyError(task_id)
        directory = self.root / task_id
        if directory.is_symlink():
            raise KeyError(task_id)
        return directory

    @staticmethod
    def write(path, body):
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("w", encoding="utf-8") as stream:
                json.dump(body, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def create(self, filename, data, fields, content_type, kind="parse"):
        self.cleanup()
        task_id = uuid4().hex
        directory = self.directory(task_id)
        directory.mkdir(parents=True, mode=0o700)
        (directory / "input.bin").write_bytes(data)
        now = time.time()
        job = {"task_id": task_id, "kind": kind, "status": "queuing", "file_name": filename,
               "fields": [(k,v) for k,v in fields if k != "api_key"], "content_type": content_type,
               "ticket": secrets.token_urlsafe(32), "created_at": now, "expires_at": now+self.ttl,
               "result": None, "metadata": {}, "error": None}
        self.write(directory / "job.json", job)
        return job

    def get(self, task_id):
        try:
            job = json.loads((self.directory(task_id) / "job.json").read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            raise KeyError(task_id) from None
        now = time.time()
        if now > job["expires_at"]:
            raise KeyError(task_id)
        # Report interrupted/undelivered work honestly; never fabricate success or
        # replay an inference automatically after an unknown outcome.
        if job["status"] == "processing" and now > job.get("started_at", now)+self.deadline:
            job = dict(job, status="failed", error="Worker interrupted or execution deadline exceeded; submit a new task")
        elif job["status"] == "queuing" and now > job["created_at"]+self.queue_ttl:
            job = dict(job, status="failed", error="Asynchronous invocation was not completed within queue deadline")
        return job

    def claim(self, job):
        directory = self.directory(job["task_id"])
        try:
            (directory / "claim").mkdir()
        except FileExistsError:
            return False
        # Atomic mkdir works across NFS clients; no SQLite/WAL or local-only lock.
        current = self.get(job["task_id"])
        if current["status"] != "queuing":
            return False
        current.update(status="processing", started_at=time.time())
        self.write(directory / "job.json", current)
        return True

    def finish(self, job, response):
        directory = self.directory(job["task_id"])
        current = self.get(job["task_id"])
        if current["status"] != "processing":
            return
        data = response.get("data") or {}
        if response.get("code") == 0 and data.get("error") is None:
            current.update(status="success", result=data.get("result"), metadata=data.get("metadata", {}), error=None)
        else:
            current.update(status="failed", error=response.get("message", "Document parsing failed"))
        current["finished_at"] = time.time()
        self.write(directory / "job.json", current)
        (directory / "input.bin").unlink(missing_ok=True)

    def fail(self, job, message):
        current = self.get(job["task_id"])
        if current["status"] not in {"success", "failed"}:
            current.update(status="failed", error=message, finished_at=time.time())
            self.write(self.directory(job["task_id"]) / "job.json", current)
            (self.directory(job["task_id"]) / "input.bin").unlink(missing_ok=True)

    def public(self, job):
        return {"record_id": None, **{key:job.get(key) for key in
                ("task_id", "status", "file_name", "result", "error", "metadata")}}


async def dispatch(job, client):
    base = os.environ.get("OXR_FC_ASYNC_BASE_URL", "").rstrip("/")
    token = os.environ.get("OXR_FC_ASYNC_TOKEN", "")
    if not base.startswith("https://") or not token:
        raise RuntimeError("FC async endpoint/token not configured")
    # Caller-supplied URLs/headers are never used. Body is a small reference, not
    # the document; this stays below FC's 128 KiB async request limit.
    response = await client.post(base+"/internal/tasks/run",
        headers={"Authorization": "Bearer "+token, "X-Fc-Invocation-Type": "Async"},
        json={"task_id": job["task_id"], "ticket": job["ticket"]}, timeout=15,
        follow_redirects=False)
    if response.status_code != 202:
        raise RuntimeError("FC did not accept asynchronous invocation")
    return response.headers.get("X-Fc-Request-Id")
