from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tempfile
import uuid
import zipfile
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.background import BackgroundTask

from .db import FarmDatabase
from .models import FailureResult, JobParams

CHUNK_SIZE = 1024 * 1024
DEFAULT_MIN_FREE_BYTES = 5 * 1024**3
PACKAGE_DIR = Path(__file__).resolve().parent


async def stream_upload(upload: UploadFile, destination: Path) -> str:
    digest = hashlib.sha256()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        while chunk := await upload.read(CHUNK_SIZE):
            digest.update(chunk)
            output.write(chunk)
    await upload.close()
    return digest.hexdigest()


def create_app(
    *,
    data_dir: str | Path | None = None,
    token: str | None = None,
    lease_seconds: int = 1800,
    sweep_interval: float = 30,
    min_free_bytes: int = DEFAULT_MIN_FREE_BYTES,
) -> FastAPI:
    root = Path(data_dir or os.getenv("FARMHAND_DATA_DIR", PACKAGE_DIR)).resolve()
    farm_token = token if token is not None else os.getenv("FARMHAND_TOKEN", "change-me")
    blend_dir = root / "storage" / "blends"
    frame_dir = root / "storage" / "frames"
    blend_dir.mkdir(parents=True, exist_ok=True)
    frame_dir.mkdir(parents=True, exist_ok=True)
    database = FarmDatabase(root / "farm.db", lease_seconds=lease_seconds)

    async def sweep_loop() -> None:
        while True:
            await asyncio.sleep(sweep_interval)
            database.sweep_expired_leases()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = asyncio.create_task(sweep_loop())
        try:
            yield
        finally:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            database.close()

    application = FastAPI(title="Farmhand", version="0.1.0", lifespan=lifespan)
    application.state.db = database
    application.state.data_dir = root

    @application.middleware("http")
    async def require_token(request: Request, call_next):
        public_paths = {"/", "/favicon.ico"}
        if (
            request.url.path not in public_paths
            and request.headers.get("X-Farm-Token") != farm_token
        ):
            return JSONResponse({"detail": "Invalid or missing farm token"}, status_code=401)
        return await call_next(request)

    @application.exception_handler(RequestValidationError)
    async def request_validation_handler(_request: Request, exc: RequestValidationError):
        return JSONResponse({"detail": exc.errors()}, status_code=422)

    @application.get("/", include_in_schema=False)
    async def dashboard():
        return FileResponse(PACKAGE_DIR / "static" / "status.html", media_type="text/html")

    @application.get("/favicon.ico", include_in_schema=False, status_code=204)
    async def favicon():
        return Response(status_code=204)

    @application.get("/jobs")
    async def list_jobs():
        return database.list_jobs()

    @application.post("/jobs", status_code=status.HTTP_201_CREATED)
    async def submit_job(
        blend_file: Annotated[UploadFile, File()], params: Annotated[str, Form()]
    ):
        if shutil.disk_usage(root).free < min_free_bytes:
            raise HTTPException(507, "Coordinator has less than 5GB free")
        try:
            parsed = JobParams.model_validate(json.loads(params))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise HTTPException(422, f"Invalid job params: {exc}") from exc
        job_id = uuid.uuid4().hex
        destination = blend_dir / f"{job_id}.blend"
        try:
            digest = await stream_upload(blend_file, destination)
            database.create_job(job_id, parsed, digest)
        except Exception:
            destination.unlink(missing_ok=True)
            raise
        return {"job_id": job_id}

    @application.get("/jobs/{job_id}")
    async def job_status(job_id: str):
        job = database.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return job

    @application.post("/jobs/{job_id}/cancel")
    async def cancel_job(job_id: str):
        if not database.cancel_job(job_id):
            raise HTTPException(404, "Active job not found")
        return {"status": "cancelled"}

    @application.get("/work")
    async def get_work(worker_id: str, blender_version: str):
        work = database.claim_work(worker_id, blender_version)
        return work if work else Response(status_code=204)

    @application.get("/jobs/{job_id}/blend")
    async def download_blend(job_id: str):
        digest = database.get_blend_digest(job_id)
        path = blend_dir / f"{job_id}.blend"
        if not digest or not path.exists():
            raise HTTPException(404, "Blend not found")
        return FileResponse(
            path,
            media_type="application/octet-stream",
            filename=path.name,
            headers={"X-Blend-SHA256": digest},
        )

    @application.post("/jobs/{job_id}/frames/{frame}/result")
    async def submit_result(job_id: str, frame: int, request: Request):
        content_type = request.headers.get("content-type", "")
        if content_type.startswith("application/json"):
            try:
                failure = FailureResult.model_validate(await request.json())
            except (ValidationError, json.JSONDecodeError) as exc:
                raise HTTPException(422, f"Invalid failure payload: {exc}") from exc
            if not database.fail_frame(job_id, frame, failure.worker_id, failure.stderr_tail):
                raise HTTPException(409, "Worker no longer holds this frame lease")
            return {"status": "recorded"}

        form = await request.form()
        worker_id = str(form.get("worker_id") or "")
        frame_file = form.get("frame_file")
        if not worker_id or not hasattr(frame_file, "read"):
            raise HTTPException(422, "worker_id and frame_file are required")
        try:
            render_seconds = float(form["render_seconds"]) if form.get("render_seconds") else None
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, "render_seconds must be a number") from exc
        job = database.get_job(job_id)
        held = job and any(
            item["frame"] == frame
            and item["state"] == "rendering"
            and item["worker_id"] == worker_id
            for item in job["frames"]
        )
        if not held:
            raise HTTPException(409, "Worker no longer holds this frame lease")
        destination = frame_dir / job_id / f"frame_{frame:04d}.png"
        await stream_upload(frame_file, destination)
        if destination.stat().st_size == 0:
            destination.unlink(missing_ok=True)
            raise HTTPException(422, "Rendered frame is empty")
        if not database.complete_frame(job_id, frame, worker_id, render_seconds):
            destination.unlink(missing_ok=True)
            raise HTTPException(409, "Worker no longer holds this frame lease")
        return {"status": "done"}

    @application.post("/jobs/{job_id}/frames/{frame}/requeue")
    async def requeue_frame(job_id: str, frame: int):
        if not database.requeue_frame(job_id, frame):
            raise HTTPException(409, "Only failed frames can be requeued")
        return {"status": "pending"}

    @application.get("/jobs/{job_id}/frames.zip")
    async def download_frames(job_id: str):
        job = database.get_job(job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        completed = [item for item in job["frames"] if item["state"] == "done"]
        handle, archive_name = tempfile.mkstemp(prefix=f"farmhand-{job_id}-", suffix=".zip")
        os.close(handle)
        archive_path = Path(archive_name)
        with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in completed:
                source = frame_dir / job_id / f"frame_{item['frame']:04d}.png"
                if source.exists():
                    archive.write(source, source.name)
        return FileResponse(
            archive_path,
            media_type="application/zip",
            filename=f"{job_id}-frames.zip",
            background=BackgroundTask(archive_path.unlink, missing_ok=True),
        )

    return application


app = create_app()
