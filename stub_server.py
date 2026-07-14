"""Tiny Farmhand coordinator stub for end-to-end worker checks."""

from __future__ import annotations

import argparse
import hashlib
import os
import threading
from collections import deque
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
JOB_ID = "00000000000000000000000000000001"


def create_app(
    blend_path: Path,
    *,
    frames: list[int] | tuple[int, ...] = (1,),
    token: str = "pick-a-long-random-string",
    output_dir: Path = Path("stub-results"),
) -> FastAPI:
    app = FastAPI(title="Farmhand worker stub")
    pending = deque(int(frame) for frame in frames)
    state: dict[str, object] = {"active": None, "completed": [], "failures": []}
    lock = threading.Lock()

    def authenticate(request: Request) -> None:
        if request.headers.get("X-Farm-Token") != token:
            raise HTTPException(status_code=401, detail="invalid farm token")

    @app.get("/work")
    def claim_work(request: Request, worker_id: str, blender_version: str):
        authenticate(request)
        if not blend_path.is_file():
            raise HTTPException(status_code=503, detail=f"blend file not found: {blend_path}")
        with lock:
            if state["active"] is not None or not pending:
                return Response(status_code=204)
            frame = pending.popleft()
            state["active"] = {"frame": frame, "worker_id": worker_id}
        digest = hashlib.sha256(blend_path.read_bytes()).hexdigest()
        return {
            "job_id": JOB_ID,
            "frame": frame,
            "blend_sha256": digest,
            "blend_url": "/blend",
            "output_format": "PNG",
            "engine": "CYCLES",
            "lease_seconds": 1800,
        }

    @app.get("/blend")
    def download_blend(request: Request):
        authenticate(request)
        if not blend_path.is_file():
            raise HTTPException(status_code=404, detail="blend file not found")
        digest = hashlib.sha256(blend_path.read_bytes()).hexdigest()
        return FileResponse(
            blend_path,
            media_type="application/octet-stream",
            headers={"X-Blend-SHA256": digest},
        )

    @app.post("/jobs/{job_id}/frames/{frame}/result")
    async def submit_result(job_id: str, frame: int, request: Request):
        authenticate(request)
        with lock:
            active = state["active"]
            if job_id != JOB_ID or not isinstance(active, dict) or active["frame"] != frame:
                raise HTTPException(status_code=409, detail="worker no longer holds this frame")
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            form = await request.form()
            upload = form.get("frame_file")
            if upload is None:
                raise HTTPException(status_code=422, detail="frame_file is required")
            contents = await upload.read()
            if len(contents) <= len(PNG_SIGNATURE) or not contents.startswith(PNG_SIGNATURE):
                raise HTTPException(status_code=400, detail="invalid PNG")
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / f"frame_{frame:04d}.png").write_bytes(contents)
            with lock:
                state["active"] = None
                state["completed"].append(frame)
            return {"status": "done"}
        payload = await request.json()
        if payload.get("status") != "failed":
            raise HTTPException(status_code=422, detail="expected failed result")
        with lock:
            state["active"] = None
            state["failures"].append({"frame": frame, **payload})
        return JSONResponse({"status": "failed"})

    @app.get("/state")
    def inspect_state(request: Request):
        authenticate(request)
        with lock:
            return {**state, "pending": list(pending)}

    return app


def _frames(value: str) -> list[int]:
    return [int(item.strip()) for item in value.split(",") if item.strip()]


app = create_app(
    Path(os.getenv("FARMHAND_BLEND", "stub.blend")),
    frames=_frames(os.getenv("FARMHAND_FRAMES", "1")),
    token=os.getenv("FARMHAND_TOKEN", "pick-a-long-random-string"),
    output_dir=Path(os.getenv("FARMHAND_RESULTS", "stub-results")),
)


def main() -> None:
    import uvicorn

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--blend", type=Path, required=True)
    parser.add_argument("--frames", type=_frames, default=[1])
    parser.add_argument("--token", default="pick-a-long-random-string")
    parser.add_argument("--output-dir", type=Path, default=Path("stub-results"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8420)
    args = parser.parse_args()
    uvicorn.run(
        create_app(args.blend, frames=args.frames, token=args.token, output_dir=args.output_dir),
        host=args.host,
        port=args.port,
    )


if __name__ == "__main__":
    main()
