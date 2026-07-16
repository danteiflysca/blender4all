from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator


class JobParams(BaseModel):
    name: str = Field(min_length=1, max_length=240)
    frame_start: int
    frame_end: int
    frame_step: int = Field(default=1, gt=0)
    output_format: Literal["PNG"] = "PNG"
    engine: Literal["CYCLES", "BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"] = "CYCLES"
    blender_version: str = Field(min_length=3, max_length=40)
    # Shared-storage mode: workers open this path directly instead of downloading.
    blend_path: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def validate_range(self) -> JobParams:
        if self.frame_end < self.frame_start:
            raise ValueError("frame_end must be greater than or equal to frame_start")
        count = ((self.frame_end - self.frame_start) // self.frame_step) + 1
        if count > 10_000:
            raise ValueError("frame range cannot exceed 10,000 frames")
        return self


class FailureResult(BaseModel):
    status: Literal["failed"]
    worker_id: str = Field(min_length=1, max_length=160)
    exit_code: int
    stderr_tail: str = Field(default="", max_length=4096)
